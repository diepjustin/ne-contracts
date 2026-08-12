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
import base64
import csv
import datetime
import json
import gzip
import os
import random
import sys
import zlib
from urllib.parse import urlparse, parse_qs, quote, unquote

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
        # load_progress returns (finished combos, partial positions). Binding the
        # pair to one name makes every membership test false and reports the whole
        # state as uncollected -- which is exactly the wrong direction for a note
        # whose job is to stop readers misreading a gap.
        done, _partial = load_progress(os.path.join(ROOT, progress_file(dataset)))
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

    orig = sum(os.path.getsize(p) for p in DATA.values())
    if duplicates:
        print(f"duplicate rows: {duplicates:,} dropped (a resumed scrape redid a page)")

    if args.emit_json:
        verify_urls(payload["url"], payload["vtok"], payload["rows"], sources)
        blob = json.dumps(payload, separators=(",", ":")).encode()
        os.makedirs(os.path.dirname(args.emit_json) or ".", exist_ok=True)
        with open(args.emit_json, "wb") as f:
            f.write(blob)
        gz = len(gzip.compress(blob, 9))
        print(f"rows          : {n:,}")
        print(f"vendors       : {len(vendors):,}")
        print(f"{args.emit_json:<14}: {len(blob) / 1e6:6.2f} MB")
        print(f"gzipped       : {gz / 1e6:6.2f} MB")
        return

    # --- binary payload ---
    build_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(OUT_DIR, "d", build_id)

    dn_bytes = [base64.b64decode(t) if t else b"" for t in dn_tokens]
    view_bytes = [base64.b64decode(unquote(t)) if t else b"" for t in view_tokens]
    vtok_bytes = [base64.b64decode(vendor_tokens.get(i, "")) for i in range(len(vendors))]

    # All but a handful of DN tokens decode to exactly TOKEN_BYTES. The rest
    # would otherwise force every record wider, so they travel in meta instead.
    dn_exceptions = {str(i): dn_tokens[i] for i, b in enumerate(dn_bytes)
                     if len(b) != ne_format.TOKEN_BYTES}
    if len(dn_exceptions) > n * 0.001:
        sys.exit(f"{len(dn_exceptions):,} DN tokens are not {ne_format.TOKEN_BYTES} bytes — "
                 "too many for meta.dnExceptions; the block format needs a length array")
    for b in view_bytes:
        if b and len(b) != ne_format.TOKEN_BYTES:
            sys.exit(f"a view token decodes to {len(b)} bytes, expected {ne_format.TOKEN_BYTES}")

    meta = dict(payload["meta"])
    meta.update({
        "formatVersion": ne_format.FORMAT_VERSION,
        "buildId": build_id,
        "endian": "little",
        "vendorCount": len(vendors),
        "blockSize": ne_format.BLOCK_ROWS,
        "blockCount": ne_format.block_count(n),
        "tokenBytes": ne_format.TOKEN_BYTES,
        "docAlphabet": "".join(sorted({c for d in docs for c in d.decode()})),
        "dnExceptions": dn_exceptions,
        "detailBase": DETAIL_BASE,
        "viewBase": VIEW_BASE,
        "adn": adn,
        "DT": payload["url"]["DT"],
        "digests": {name: zlib.crc32(col.tobytes()) for name, col in columns.items()},
    })

    vendor_names = [v.encode("utf-8") for v in payload["vendors"]]
    for v in vendor_names:
        if len(v) > 255:
            sys.exit(f"vendor name exceeds 255 bytes — the length column is a u8; widen it")

    # Document numbers and vendor names are not columns, so the per-column
    # digests above leave the two largest resident files unchecked in the
    # browser. Digest them whole, by file, so ?selftest=1 covers everything it
    # loads. Keyed by filename to stay distinguishable from column names.
    meta["digests"][ne_format.DOCS] = zlib.crc32(b"".join(docs))
    meta["digests"][ne_format.VENDORS] = zlib.crc32(
        bytes(len(v) for v in vendor_names) + b"".join(vendor_names))

    ne_format.write_payload(outdir, columns, docs, vendor_names, vtok_bytes, meta)
    ne_format.write_token_blocks(outdir, dn_bytes, view_bytes, n)
    write_selftest(outdir, sources, columns["viewPresent"], n)
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"buildId": build_id, "dir": f"d/{build_id}"}, f)

    verify_payload(outdir, columns, docs, vendor_names, vtok_bytes,
                   dn_tokens, view_tokens, sources)

    resident = sum(os.path.getsize(os.path.join(outdir, p)) for p in
                   (ne_format.META, ne_format.COLS_I32, ne_format.COLS_F64,
                    ne_format.COLS_U8, ne_format.DOCS, ne_format.VENDORS))
    tok = sum(os.path.getsize(ne_format.block_path(outdir, b))
              for b in range(ne_format.block_count(n))) + os.path.getsize(
                  os.path.join(outdir, ne_format.VTOK))
    print(f"rows          : {n:,}")
    print(f"vendors       : {len(vendors):,}")
    print(f"source CSVs   : {orig / 1e6:6.2f} MB")
    print(f"resident      : {resident / 1e6:6.2f} MB raw  (loaded up front)")
    print(f"deferred      : {tok / 1e6:6.2f} MB raw in {ne_format.block_count(n):,} blocks "
          f"(fetched on click)")
    print(f"written to    : {outdir}")


SELFTEST_ROWS = 1000


def write_selftest(outdir, sources, view_present, n):
    """A fixture of row -> URL pairs, taken from the CSVs, for `?selftest=1`.

    Python's verification proves the *files* carry the source data. It says
    nothing about the JavaScript that decodes them — a swapped section, a
    mis-sized block, a base64 slip would all pass on this side and produce
    wrong links in the browser. So ship a sample of the scraped truth and let
    the page check itself against it, on the real CDN where gzip-in-transit
    and range behavior actually apply.

    The seed is fixed so a rebuild of unchanged data samples the same rows,
    which makes two runs comparable.
    """
    rows = random.Random(20260811).sample(range(n), min(SELFTEST_ROWS, n))
    fixture = []
    for i in sorted(rows):
        detail, view = sources[i]
        # The page shows the view URL when there is one, so that is what it
        # must be held to; view_present is the column it decides on.
        fixture.append([i, view if view_present[i] else detail])

    with open(os.path.join(outdir, ne_format.SELFTEST), "w", encoding="utf-8") as f:
        json.dump({"rows": fixture}, f, separators=(",", ":"))


def verify_urls(url, vtok, rows, sources, sample_out=None):
    """Rebuild every URL from the payload and assert it matches the original exactly.

    The check that earns its keep: it caught A, D and N being assumed
    entity-determined, which held at two entities and again across five but is
    wrong at 92. It compares against the URLs as scraped, never against
    anything derived from the payload.
    """
    checked = 0
    for row, (detail, view) in zip(rows, sources):
        _, vi, _, _, _, _, _, ti, dn, view_suffix, ui = row

        if detail:
            a, d, nn = url["adn"][ui]
            rebuilt = (
                f"{url['detailBase']}?A={quote(a, safe='')}"
                f"&D={quote(d, safe='')}"
                f"&DN={quote(dn, safe='')}"
                f"&N={quote(nn, safe='')}"
                f"&DT={quote(url['DT'][ti], safe='')}"
                f"&V={quote(vtok[vi], safe='')}"
            )
            if rebuilt != detail:
                sys.exit(f"URL round-trip FAILED\n  original: {detail}\n  rebuilt : {rebuilt}")
            checked += 1
            if sample_out is not None and len(sample_out) < 5:
                sample_out.append(rebuilt)

        if view:
            if url["viewBase"] + view_suffix != view:
                sys.exit(f"view URL round-trip FAILED\n  original: {view}")
            checked += 1

    print(f"round-trip verified: {checked:,} URLs reconstruct exactly")
    return checked


def verify_payload(outdir, columns, docs, vendor_names, vtok_bytes,
                   dn_tokens, view_tokens, sources):
    """Decode the written files and prove they carry exactly the same data.

    Reads from disk rather than from the objects just built, so an encoder bug
    -- wrong section order, wrong stride, an off-by-one, the wrong endianness
    -- fails here instead of shipping.
    """
    dec_cols, dec_docs, dec_vendors, dec_vtok, dec_meta = ne_format.read_payload(outdir)
    n = dec_meta["count"]

    for name, col in columns.items():
        if name not in dec_cols:
            sys.exit(f"column {name!r} missing after decode")
        # Bytes, not values: float equality would quietly accept a changed
        # amount that happens to compare equal.
        if dec_cols[name].tobytes() != col.tobytes():
            sys.exit(f"column {name!r} does not survive the round trip")
    if dec_docs != docs:
        sys.exit("document numbers do not survive the round trip")
    if dec_vendors != vendor_names:
        sys.exit("vendor names do not survive the round trip")
    if dec_vtok != vtok_bytes:
        sys.exit("vendor tokens do not survive the round trip")

    dec_dn, dec_view = ne_format.read_token_blocks(outdir, n)
    exceptions = dec_meta["dnExceptions"]
    for i in range(n):
        want_dn = dn_tokens[i]
        got_dn = exceptions.get(str(i)) or base64.b64encode(dec_dn[i]).decode()
        if got_dn != want_dn:
            sys.exit(f"row {i}: DN token round trip failed ({got_dn!r} != {want_dn!r})")
        want_view = view_tokens[i]
        got_view = quote(base64.b64encode(dec_view[i]).decode(), safe="") if want_view else ""
        if got_view != want_view:
            sys.exit(f"row {i}: view token round trip failed ({got_view!r} != {want_view!r})")

    # The strongest single statement: the decoded files rebuild the same rows
    # the JSON path produced. Chunked so peak memory stays bounded.
    url = {"detailBase": dec_meta["detailBase"], "viewBase": dec_meta["viewBase"],
           "adn": [list(x) for x in dec_meta["adn"]], "DT": dec_meta["DT"]}
    vtok_text = [base64.b64encode(v).decode() for v in dec_vtok]
    samples = []
    total = 0
    for lo in range(0, n, 100_000):
        hi = min(lo + 100_000, n)
        sl = {k: v[lo:hi] for k, v in dec_cols.items()}
        rebuilt = build_rows(sl, dec_docs[lo:hi],
                             [exceptions.get(str(i)) or base64.b64encode(dec_dn[i]).decode()
                              for i in range(lo, hi)],
                             view_tokens[lo:hi])
        reference = build_rows({k: v[lo:hi] for k, v in columns.items()}, docs[lo:hi],
                               dn_tokens[lo:hi], view_tokens[lo:hi])
        if rebuilt != reference:
            sys.exit(f"rows {lo}..{hi} differ after decoding the binary payload")
        total += verify_urls(url, vtok_text, rebuilt, sources[lo:hi], samples)
    print(f"decoded payload matches the source for all {n:,} rows "
          f"({total:,} URLs re-verified from disk)")

    print("\nspot-check these by hand — a payload that round-trips against dead links is still broken:")
    for s in samples:
        print(f"  {s}")
    print()


if __name__ == "__main__":
    main()
