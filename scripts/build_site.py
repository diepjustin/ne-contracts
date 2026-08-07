"""Normalize the scraped CSVs into a compact JSON payload for the static search site.

The raw CSVs are ~76% URLs, most of it repeated on every row. The Nebraska detail
URL has six query params, and only two actually vary per record:

    A   one global constant
    D   determined by entity (UNL vs Central Administration)
    N   determined by entity
    DT  determined by document type (Contract vs Purchase Order)
    DN  unique per row
    V   1:1 with vendor, so derivable from the vendor index

So we ship the varying parts and rebuild the rest in the browser. Every URL is
round-trip verified against the original before the payload is written.
"""

import csv
import datetime
import json
import gzip
import os
import sys
from urllib.parse import urlparse, parse_qs

# Paths resolve against the project folder (the parent of scripts/), so the
# scripts work no matter which directory they are invoked from. index.html and
# data.json sit at the folder root because that root is the published URL.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA = {
    "Contract": os.path.join(ROOT, "data", "nu_contracts.csv"),
    "Purchase Order": os.path.join(ROOT, "data", "nu_purchase_orders.csv"),
}
OUT_DIR = ROOT
OUT_JSON = os.path.join(OUT_DIR, "data.json")

DETAIL_BASE = "https://statecontracts.nebraska.gov/Search/SearchDocuments"
VIEW_BASE = "https://statecontracts.nebraska.gov/Search/ViewDocument?D="

ENTITIES = ["University of Nebraska Lincoln", "University of Nebraska Central Administration"]
STATUSES = ["Active", "Expired"]
TYPES = ["Contract", "Purchase Order"]


def to_ymd(mdy):
    """MM/DD/YYYY -> integer YYYYMMDD, or 0 if unparseable."""
    parts = mdy.split("/")
    if len(parts) != 3:
        return 0
    m, d, y = parts
    try:
        return int(y) * 10000 + int(m) * 100 + int(d)
    except ValueError:
        return 0


def to_amount(raw):
    try:
        return round(float(raw.replace("$", "").replace(",", "").strip()), 2)
    except ValueError:
        return 0.0


def main():
    for path in DATA.values():
        if not os.path.exists(path):
            sys.exit(f"missing {path} — run the scraper first")

    vendors = {}       # name -> index
    vendor_tokens = {} # index -> V param
    const_D = {}       # entity index -> D
    const_N = {}       # entity index -> N
    const_DT = {}      # type index -> DT
    const_A = set()

    rows = []
    sources = []       # parallel to rows: original URLs, for round-trip verification

    for doc_type, path in DATA.items():
        ti = TYPES.index(doc_type)
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                ent = r["Entity Name"]
                if ent not in ENTITIES:
                    sys.exit(f"unexpected entity {ent!r} in {path}")
                ei = ENTITIES.index(ent)
                si = STATUSES.index(r["Status"])

                name = r["Vendor"]
                if name not in vendors:
                    vendors[name] = len(vendors)
                vi = vendors[name]

                dn = ""
                detail = r["Detail URL"]
                if detail:
                    q = parse_qs(urlparse(detail).query)
                    dn = q.get("DN", [""])[0]
                    const_A.add(q.get("A", [""])[0])
                    const_D.setdefault(ei, q.get("D", [""])[0])
                    const_N.setdefault(ei, q.get("N", [""])[0])
                    const_DT.setdefault(ti, q.get("DT", [""])[0])
                    v = q.get("V", [""])[0]
                    if vi in vendor_tokens and vendor_tokens[vi] != v:
                        sys.exit(f"vendor {name!r} maps to two V tokens — assumption broken")
                    vendor_tokens[vi] = v

                view = r["View URL"]
                if view and not view.startswith(VIEW_BASE):
                    sys.exit(f"unexpected view URL shape: {view[:80]}")

                rows.append([
                    r["Document Number"],
                    vi,
                    to_amount(r["Amount"]),
                    to_ymd(r["Begin Date"]),
                    to_ymd(r["End Date"]),
                    si,
                    ei,
                    ti,
                    dn,
                    view[len(VIEW_BASE):] if view else "",
                ])
                sources.append((detail, view))

    if len(const_A) != 1:
        sys.exit(f"expected exactly one A constant, got {len(const_A)}")
    A = const_A.pop()

    payload = {
        "meta": {
            "entities": ENTITIES,
            "statuses": STATUSES,
            "types": TYPES,
            "count": len(rows),
            "built": datetime.date.today().isoformat(),
        },
        "url": {
            "detailBase": DETAIL_BASE,
            "viewBase": VIEW_BASE,
            "A": A,
            "D": [const_D[i] for i in range(len(ENTITIES))],
            "N": [const_N[i] for i in range(len(ENTITIES))],
            "DT": [const_DT[i] for i in range(len(TYPES))],
        },
        "vendors": [n for n, _ in sorted(vendors.items(), key=lambda kv: kv[1])],
        "vtok": [vendor_tokens.get(i, "") for i in range(len(vendors))],
        "rows": rows,
    }

    verify(payload, sources)

    os.makedirs(OUT_DIR, exist_ok=True)
    blob = json.dumps(payload, separators=(",", ":")).encode()
    with open(OUT_JSON, "wb") as f:
        f.write(blob)

    orig = sum(os.path.getsize(p) for p in DATA.values())
    gz = len(gzip.compress(blob, 9))
    print(f"rows          : {len(rows):,}")
    print(f"vendors       : {len(vendors):,}")
    print(f"source CSVs   : {orig / 1e6:6.2f} MB")
    print(f"{OUT_JSON:<14}: {len(blob) / 1e6:6.2f} MB")
    print(f"gzipped       : {gz / 1e6:6.2f} MB  ({gz / orig * 100:.0f}% of source)")


def verify(payload, sources):
    """Rebuild every URL from the payload and assert it matches the original exactly."""
    from urllib.parse import quote

    u = payload["url"]
    checked = 0
    for row, (detail, view) in zip(payload["rows"], sources):
        _, vi, _, _, _, _, ei, ti, dn, vtok = row

        if detail:
            rebuilt = (
                f"{u['detailBase']}?A={quote(u['A'], safe='')}"
                f"&D={quote(u['D'][ei], safe='')}"
                f"&DN={quote(dn, safe='')}"
                f"&N={quote(u['N'][ei], safe='')}"
                f"&DT={quote(u['DT'][ti], safe='')}"
                f"&V={quote(payload['vtok'][vi], safe='')}"
            )
            if rebuilt != detail:
                sys.exit(f"URL round-trip FAILED\n  original: {detail}\n  rebuilt : {rebuilt}")
            checked += 1

        if view:
            if u["viewBase"] + vtok != view:
                sys.exit(f"view URL round-trip FAILED\n  original: {view}")
            checked += 1

    print(f"round-trip verified: {checked:,} URLs reconstruct exactly\n")


if __name__ == "__main__":
    main()
