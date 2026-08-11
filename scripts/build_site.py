"""Normalize the scraped CSVs into a compact JSON payload for the static search site.

The raw CSVs are ~76% URLs, most of it repeated on every row. The Nebraska detail
URL has six query params, and only two actually vary per record:

    A, D, N   vary together, but not by anything we can name
    DT        determined by document type
    DN        unique per row
    V         1:1 with vendor, so derivable from the vendor index

A, D and N looked entity-determined at two-entity scale and again across five,
but at full scale twelve agencies carry more than one N and three N values span
agencies -- so any rule stated in terms of entity is wrong somewhere. Rather
than guess a better key, the three are stored as one deduplicated table of the
combinations that actually occur (98 of them across 295,895 rows) with a small
index per row. That is correct whatever the state keys them on.

So we ship the varying parts and rebuild the rest in the browser. Every URL is
round-trip verified against the original before the payload is written.

Document types are discovered from the data rather than declared. Higher
Education uses two readable ones ("Contract", "Purchase Order"); State agencies
use the source system's internal codes ("O4", "ZO", "OP", ...), and how many
exist isn't knowable up front. DT stays keyed by type alone -- verified against
live data that a given code maps to the same DT in every agency.
"""

import argparse
import array
import csv
import datetime
import json
import gzip
import os
import sys
from urllib.parse import urlparse, parse_qs

import ne_format

# Paths resolve against the project folder (the parent of scripts/), so the
# scripts work no matter which directory they are invoked from. index.html and
# data.json sit at the folder root because that root is the published URL.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Keyed by dataset name, matching the stamps scrape.py writes to scrape_meta.json.
DATA = {
    "contract": os.path.join(ROOT, "data", "nu_contracts.csv"),
    "purchase-order": os.path.join(ROOT, "data", "nu_purchase_orders.csv"),
    "state": os.path.join(ROOT, "data", "state_agencies.csv"),
}
SCRAPE_META = os.path.join(ROOT, "data", "scrape_meta.json")
OUT_DIR = ROOT
OUT_JSON = os.path.join(OUT_DIR, "data.json")

DETAIL_BASE = "https://statecontracts.nebraska.gov/Search/SearchDocuments"
VIEW_BASE = "https://statecontracts.nebraska.gov/Search/ViewDocument?D="

def load_entities():
    """Every entity the scraper knows about, alphabetized.

    Sorted here rather than in the page: index.html renders the dropdown in
    whatever order this list arrives in, so sorting once at build time is all
    the ordering the UI needs.
    """
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from scrape import HIGHER_ED_ENTITIES, STATE_ENTITIES  # noqa: E402

    return sorted(set(HIGHER_ED_ENTITIES) | set(STATE_ENTITIES))


ENTITIES = load_entities()
STATUSES = ["Active", "Expired"]


def type_groups(codes):
    """Each code's category ("Contract" / "Purchase Order"), parallel to `codes`.

    The raw code stays the canonical value -- it keys the DT lookup and shows in
    the table -- but nobody can filter on "Z8", so the page offers these instead.
    A code missing from the map groups as itself rather than being guessed at.
    """
    with open(os.path.join(ROOT, "scripts", "type_groups.json"), encoding="utf-8") as f:
        mapping = {k: v for k, v in json.load(f).items() if not k.startswith("_")}

    unknown = [c for c in codes if c not in mapping]
    if unknown:
        print(f"warning: no group for document type(s) {unknown} — "
              f"add them to scripts/type_groups.json; they will filter under their raw code")
    return [mapping.get(c, c) for c in codes]


def incomplete_coverage():
    """Datasets still missing entities, as ["<entity> (<label>)", ...].

    Read from the scrapers' own checkpoints rather than declared by hand, so
    the page stops warning about a gap the moment that gap is actually filled.
    Without this, an entity whose scrape never finished looks identical to one
    the state simply has no records for -- which would quietly invite the wrong
    conclusion about an agency's spending.
    """
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from scrape import (HIGHER_ED_ENTITIES, STATE_ENTITIES, STATUSES,  # noqa: E402
                        load_progress, progress_file)

    labels = {"contract": "contracts", "purchase-order": "purchase orders", "state": "records"}
    gaps = []
    for dataset in DATA:
        entities = STATE_ENTITIES if dataset == "state" else HIGHER_ED_ENTITIES
        done = load_progress(os.path.join(ROOT, progress_file(dataset)))
        missing = sorted({e for e in entities for s in STATUSES if (e, s) not in done})
        gaps += [f"{e} ({labels[dataset]})" for e in missing]
    return gaps


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


def scraped_at():
    """When the source data was last pulled, as an ISO-8601 string, or None.

    Each document type is scraped by its own run, so the dataset is only as
    current as its stalest half — report the oldest of the completion times.
    Returns None when any type is unstamped (data predating scrape_meta.json),
    so the page can fall back rather than overstate freshness.
    """
    try:
        with open(SCRAPE_META, encoding="utf-8") as f:
            stamps = json.load(f)
    except (OSError, ValueError):
        return None

    if not set(DATA) <= set(stamps):
        return None

    try:
        # min() raises TypeError if the stamps mix offset-aware and naive times.
        oldest = min(datetime.datetime.fromisoformat(stamps[k]) for k in DATA)
    except (TypeError, ValueError):
        return None

    return oldest.isoformat(timespec="seconds")


def build_rows(columns, docs, dn_tokens, view_tokens):
    """The legacy 11-field row list, rebuilt from the columns.

    Two jobs. It backs --emit-json for the older payload, and it is the
    strongest correctness check available: if the columns cannot reproduce a
    row exactly, one of them is missing or wrong, and comparing rebuilt rows
    from the decoder against rebuilt rows from the reference says so in one
    equality.
    """
    vi = columns["vendorIdx"]; amt = columns["amount"]
    beg = columns["begin"]; end = columns["end"]
    st = columns["status"]; ent = columns["entity"]
    ty = columns["type"]; ui = columns["adnIdx"]
    return [
        [docs[i].decode("utf-8"), vi[i], amt[i], beg[i], end[i],
         st[i], ent[i], ty[i], dn_tokens[i], view_tokens[i], ui[i]]
        for i in range(len(docs))
    ]


def main():
    parser = argparse.ArgumentParser(description="Build the published payload from the scraped CSVs.")
    parser.add_argument("--emit-json", metavar="PATH", default=None,
                        help="write the legacy single-file JSON payload here instead of data.json")
    args = parser.parse_args()

    for path in DATA.values():
        if not os.path.exists(path):
            sys.exit(f"missing {path} — run the scraper first")

    vendors = {}       # name -> index
    vendor_tokens = {} # index -> V param
    const_DT = {}      # type index -> DT

    adn = []           # the (A, D, N) combinations that actually occur
    adn_index = {}

    def adn_idx(triple):
        if triple not in adn_index:
            adn_index[triple] = len(adn)
            adn.append(list(triple))
        return adn_index[triple]

    types = []         # discovered from the data, in first-seen order
    type_index = {}

    def type_idx(code):
        if code not in type_index:
            type_index[code] = len(types)
            types.append(code)
        return type_index[code]

    # Columns rather than a list of row lists: at 738,195 rows the row-list
    # shape costs ~1.4 GB of Python objects, and the payload is written
    # column-wise anyway.
    columns = {name: array.array("i") for name in ne_format.I32_COLUMNS}
    columns.update({name: array.array("d") for name in ne_format.F64_COLUMNS})
    columns.update({name: array.array("B") for name in ne_format.U8_COLUMNS})
    docs = []          # document numbers, as bytes
    dn_tokens = []     # DN, still base64 text at this point
    view_tokens = []   # view-URL suffix, still percent-encoded text

    sources = []       # parallel to the columns: original URLs, for round-trip verification

    # A scrape interrupted between writing a page and checkpointing it re-fetches
    # that page on resume, so the CSV can hold one duplicated page (~25 rows).
    # They are byte-identical, including the per-document DN token, so dropping
    # exact repeats cannot lose a distinct record.
    seen = set()
    duplicates = 0

    for path in DATA.values():
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                fingerprint = (r["Document Number"], r["Entity Name"], r["Status"],
                               r["Document Type"], r["Detail URL"])
                if fingerprint in seen:
                    duplicates += 1
                    continue
                seen.add(fingerprint)

                ent = r["Entity Name"]
                if ent not in ENTITIES:
                    sys.exit(f"unexpected entity {ent!r} in {path}")
                ei = ENTITIES.index(ent)
                si = STATUSES.index(r["Status"])
                # Per row, not per file: one State CSV mixes many type codes.
                ti = type_idx(r["Document Type"])

                name = r["Vendor"]
                if name not in vendors:
                    vendors[name] = len(vendors)
                vi = vendors[name]

                dn = ""
                ui = 0  # index into the (A, D, N) table; unused when no detail URL
                detail = r["Detail URL"]
                if detail:
                    q = parse_qs(urlparse(detail).query)
                    dn = q.get("DN", [""])[0]
                    ui = adn_idx((q.get("A", [""])[0], q.get("D", [""])[0], q.get("N", [""])[0]))
                    const_DT.setdefault(ti, q.get("DT", [""])[0])
                    v = q.get("V", [""])[0]
                    if vi in vendor_tokens and vendor_tokens[vi] != v:
                        sys.exit(f"vendor {name!r} maps to two V tokens — assumption broken")
                    vendor_tokens[vi] = v

                view = r["View URL"]
                if view and not view.startswith(VIEW_BASE):
                    sys.exit(f"unexpected view URL shape: {view[:80]}")

                doc = r["Document Number"].encode("utf-8")
                if len(doc) > 255:
                    sys.exit(f"document number {r['Document Number']!r} exceeds 255 bytes — "
                             "docLen is a u8; widen the column")
                if any(c.islower() for c in r["Document Number"]):
                    sys.exit(f"document number {r['Document Number']!r} contains lowercase — "
                             "the page case-folds against meta.docAlphabet; add a folded blob")

                view_suffix = view[len(VIEW_BASE):] if view else ""
                columns["vendorIdx"].append(vi)
                columns["amount"].append(to_amount(r["Amount"]))
                columns["begin"].append(to_ymd(r["Begin Date"]))
                columns["end"].append(to_ymd(r["End Date"]))
                columns["status"].append(si)
                columns["entity"].append(ei)
                columns["type"].append(ti)
                columns["adnIdx"].append(ui)
                columns["viewPresent"].append(1 if view_suffix else 0)
                columns["docLen"].append(len(doc))
                docs.append(doc)
                dn_tokens.append(dn)
                view_tokens.append(view_suffix)

                sources.append((detail, view))

    n = len(docs)

    # The u8 columns index dictionaries that are small today but not bounded by
    # anything except the state's own data. Fail here rather than silently
    # wrapping a value and mislabelling every affected row.
    for name, size in (("entity", len(ENTITIES)), ("type", len(types)), ("adnIdx", len(adn))):
        if size > 255:
            sys.exit(f"{name} dictionary has {size} entries — the column is a u8; widen it")

    scraped = scraped_at()
    if not scraped:
        print(f"warning: no complete scrape times in {SCRAPE_META} — page will fall back to the build date")

    payload = {
        "meta": {
            "entities": ENTITIES,
            "statuses": STATUSES,
            "types": types,
            "typeGroups": type_groups(types),
            "count": n,
            "built": datetime.date.today().isoformat(),
            "scraped": scraped,
            "incomplete": incomplete_coverage(),
        },
        "url": {
            "detailBase": DETAIL_BASE,
            "viewBase": VIEW_BASE,
            # Each entry is one [A, D, N] combination; rows carry its index.
            "adn": adn,
            # A type with no detail URL anywhere contributes nothing; the empty
            # string keeps this aligned with the type indices rows refer to.
            "DT": [const_DT.get(i, "") for i in range(len(types))],
        },
        "vendors": [name for name, _ in sorted(vendors.items(), key=lambda kv: kv[1])],
        "vtok": [vendor_tokens.get(i, "") for i in range(len(vendors))],
        "rows": build_rows(columns, docs, dn_tokens, view_tokens),
    }

    verify(payload, sources)

    out_json = args.emit_json or OUT_JSON
    os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)
    blob = json.dumps(payload, separators=(",", ":")).encode()
    with open(out_json, "wb") as f:
        f.write(blob)

    orig = sum(os.path.getsize(p) for p in DATA.values())
    gz = len(gzip.compress(blob, 9))
    if duplicates:
        print(f"duplicate rows: {duplicates:,} dropped (a resumed scrape redid a page)")
    print(f"rows          : {n:,}")
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
        _, vi, _, _, _, _, ei, ti, dn, vtok, ui = row

        if detail:
            a, d, n = u["adn"][ui]
            rebuilt = (
                f"{u['detailBase']}?A={quote(a, safe='')}"
                f"&D={quote(d, safe='')}"
                f"&DN={quote(dn, safe='')}"
                f"&N={quote(n, safe='')}"
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
