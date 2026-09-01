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
import collections
import csv
import datetime
import json
import gzip
import os
import random
import re
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
SCOPE_JSONL = os.path.join(ROOT, "data", "scope.jsonl")
OUT_DIR = ROOT
OUT_JSON = os.path.join(OUT_DIR, "data.json")

DETAIL_BASE = "https://statecontracts.nebraska.gov/Search/SearchDocuments"
VIEW_BASE = "https://statecontracts.nebraska.gov/Search/ViewDocument?D="

def load_document_counts(detail_urls, view_tokens):
    """(docCount column, row -> packed document list, drift report).

    Joins data/documents.jsonl onto the payload's rows by the hash of each
    row's detail URL. Rows the backfill has not reached get DOC_COUNT_UNKNOWN
    rather than 1: the backfill runs in sittings over ~33 hours, so a build
    between them has hundreds of thousands of unasked rows, and calling those
    "one document" would be a claim about rows nobody has looked at. See
    ne_format.DOC_COUNT_UNKNOWN.

    The row's primary document stays whatever the CSV already says. The state
    does not list documents in date order, so a newly filed one can appear
    first and shift the row's first token -- and descriptions are keyed by that
    token, so adopting the new one would orphan them. Two different things can
    happen and only the second is a problem:

      moved    the CSV's document is still published, just no longer first
      gone     the CSV's document is no longer published at all, so that
               row's description now describes a document the state has
               withdrawn

    Both are counted and reported; neither changes what is written.
    """
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from scrape import document_key, load_documents  # noqa: E402

    store = load_documents(os.path.join(ROOT, "data", "documents.jsonl"))
    counts = array.array("B", bytes([ne_format.DOC_COUNT_UNKNOWN]) * len(detail_urls))
    packed = {}
    moved = gone = clamped = 0

    for row, detail in enumerate(detail_urls):
        entry = store.get(document_key(detail))
        if entry is None:
            continue

        count = entry["n"]
        if count > 254:
            # 255 is the unknown sentinel, so a genuinely enormous list has to
            # stop at 254 rather than wrap into "nobody asked".
            clamped += 1
            count = 254
        counts[row] = count

        documents = entry.get("documents")
        if not documents:
            continue

        tokens = [d["token"] for d in documents]
        if view_tokens[row]:
            if view_tokens[row] not in tokens:
                gone += 1
            elif tokens[0] != view_tokens[row]:
                moved += 1
        packed[row] = ne_format.pack_documents(
            [{"token": base64.b64decode(unquote(d["token"])),
              "name": d["name"], "size": d["size"]} for d in documents])

    checked = sum(1 for c in counts if c != ne_format.DOC_COUNT_UNKNOWN)
    return counts, packed, {"checked": checked, "moved": moved, "gone": gone,
                            "clamped": clamped}


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
        path = os.path.join(ROOT, progress_file(dataset))

        # No checkpoint file means we do not know this dataset's coverage -- not
        # that none of it was collected. The distinction matters because the
        # three possible answers are not equally wrong: staying silent risks a
        # reader misreading a real gap, while announcing 101 false gaps tells
        # every reader that the whole state is half-collected when it is
        # finished. The second is louder and worse, and it shipped: moving the
        # build into CI left the checkpoints behind on the laptop, and the live
        # site spent 17 Aug 2026 declaring every agency "still being collected".
        if not os.path.exists(path):
            print(f"warning: {progress_file(dataset)} is missing — cannot report {dataset} "
                  "coverage, so claiming no gaps rather than claiming all of them")
            continue

        # load_progress returns (finished combos, partial positions). Binding the
        # pair to one name makes every membership test false and reports the whole
        # state as uncollected -- which is exactly the wrong direction for a note
        # whose job is to stop readers misreading a gap.
        done, _partial = load_progress(path)
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


def find_permalink_collision(docs, entity_column, type_column, view_tokens):
    """Rows that share a permalink while pointing at *different* documents.

    Returns a list of row lists, one per ambiguous group, empty when there is
    nothing to disambiguate.

    The shareable link is ?doc=&agency=&type=, and it identifies a record only
    while that triple is unique. Row ids would be shorter, but they move on
    every rebuild -- a row-id permalink silently points at a different contract
    after the next scrape. Document number alone is not enough either: 14,633
    of them are reused across agencies, covering 29,321 rows.

    The triple stopped being unique on 17 Aug 2026 and this check stopped the
    nightly publish, which is what it was for. But it was testing a proxy. Of
    176 repeated triples, 171 are the state carrying one contract under two
    vendor spellings -- same amount, same dates, same document -- so a link
    resolving to either row lands on the same PDF and nothing is wrong. Only 5
    are genuinely two documents, and those are the ones that matter: `45500` at
    UNMC is $2,558,983 expired and $21,204,743 active, and a reporter sent to
    the wrong one is quoting a superseded figure.

    So compare destinations, not triples. A repeat whose rows share a view
    token is not a collision; a repeat whose rows do not is, and the page needs
    a disambiguator for it.
    """
    groups = collections.defaultdict(list)
    for row in range(len(docs)):
        groups[(docs[row], entity_column[row], type_column[row])].append(row)
    return [rows for rows in groups.values()
            if len(rows) > 1 and len({view_tokens[r] for r in rows}) > 1]


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
    parser.add_argument("--descriptions-only", action="store_true",
                        help="attach description blocks to the build manifest.json points at, "
                             "without rebuilding it (needs data/scope.jsonl, not the CSVs)")
    args = parser.parse_args()

    if args.descriptions_only:
        add_descriptions_to_build()
        return

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
    detail_urls = []   # what documents.jsonl is keyed on, row by row

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
                detail_urls.append(detail)

                sources.append((detail, view))

    n = len(docs)

    # The u8 columns index dictionaries that are small today but not bounded by
    # anything except the state's own data. Fail here rather than silently
    # wrapping a value and mislabelling every affected row.
    for name, size in (("entity", len(ENTITIES)), ("type", len(types)), ("adnIdx", len(adn))):
        if size > 255:
            sys.exit(f"{name} dictionary has {size} entries — the column is a u8; widen it")

    # Which documents each row publishes, joined on from the backfill's log.
    # Unasked rows stay DOC_COUNT_UNKNOWN, so a half-finished backfill claims
    # nothing about the rows it has not reached.
    doc_counts, xdoc_packed, doc_drift = load_document_counts(detail_urls, view_tokens)
    columns["docCount"] = doc_counts
    if doc_drift["checked"]:
        print(f"documents     : {doc_drift['checked']:,} of {n:,} rows checked, "
              f"{sum(1 for c in doc_counts if 1 < c < 255):,} publish more than one")
        if doc_drift["moved"]:
            print(f"                {doc_drift['moved']:,} row(s) have gained a document "
                  "that now sorts ahead of the one the CSV points at (harmless: the "
                  "description still has its document)")
        if doc_drift["gone"]:
            print(f"                {doc_drift['gone']:,} row(s) no longer publish the "
                  "document their description was read from — the state withdrew it")
        if doc_drift["clamped"]:
            print(f"                {doc_drift['clamped']:,} row(s) publish more than 254 "
                  "documents; the column is a u8 and stops there")
    else:
        print(f"documents     : none of {n:,} rows checked yet — "
              "run scripts/backfill_documents.py; the page will claim nothing")

    # Rows the triple cannot tell apart. The page appends &d=<view token> to
    # their permalinks, so they stay individually addressable; every other row
    # keeps the short, readable URL. Sorted so the payload is reproducible.
    collisions = find_permalink_collision(docs, columns["entity"], columns["type"], view_tokens)
    ambiguous = sorted(row for group in collisions for row in group)
    if ambiguous:
        print(f"note: {len(ambiguous)} rows share a permalink with a different document "
              f"and will carry &d= to stay distinct")
        for row in ambiguous[:10]:
            print(f"        row {row}: {docs[row].decode()} / {ENTITIES[columns['entity'][row]]}"
                  f" / {types[columns['type'][row]]}")

        # The disambiguator is the document's own address on the state's site,
        # so a row the state publishes no document for has nothing to be named
        # by. It does not need naming: within its group, *not* carrying &d= is
        # what identifies it, and the page resolves the bare permalink to the
        # member with no document. That holds for exactly one such row. Two in
        # the same group would be genuinely indistinguishable, and shipping
        # them would mean a permalink that silently opens its twin.
        #
        # Peru State College's 80-3-3305 is the first of these: Tutor.com at
        # $5,600 twice, 2025-26 expired with no document and 2026-27 active
        # with one. It is a renewal, not a duplicate, so both rows need to stay
        # addressable. Measured across all 741,653 rows: 302 ambiguous groups,
        # one of which has a member with no document, and none has two.
        for group in collisions:
            missing = [row for row in group if not view_tokens[row]]
            if len(missing) > 1:
                sys.exit(
                    f"rows {missing[:5]} share a permalink and none of them has a view token "
                    "to be told apart by — the bare permalink can only stand for one of them, "
                    "so they need a different disambiguator")
        bare = [row for group in collisions for row in group if not view_tokens[row]]
        if bare:
            print(f"        {len(bare)} of those have no document and are addressed by the "
                  f"permalink without &d=")

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
        # Rows whose ?doc=&agency=&type= triple also names a different
        # document, mapped to the view token that tells them apart. Ten rows
        # today, so carrying the tokens outright costs nothing and keeps the
        # short URL for the other 738,185. The page cannot look these up
        # itself: view tokens live in deferred blocks it has not fetched yet.
        "ambiguous": {str(row): view_tokens[row] for row in ambiguous},
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

    descriptions, desc_sources, desc_documents = load_descriptions(view_tokens)
    meta["descCount"] = len(descriptions)
    meta["descBytes"] = sum(len(d) for d in descriptions.values())
    add_source_meta(meta, desc_sources)

    # How much of the corpus has actually been asked what it publishes. The
    # page needs this to describe its own coverage honestly while the backfill
    # is only part-way through, and the README quotes it.
    meta["docCheckedRows"] = doc_drift["checked"]
    meta["docMultiRows"] = sum(1 for c in doc_counts if 1 < c < ne_format.DOC_COUNT_UNKNOWN)
    meta["docCountUnknown"] = ne_format.DOC_COUNT_UNKNOWN

    # Keep what write_payload returns: it adds each resident file's size, and
    # the two writers below add keys of their own. meta.json is rewritten after
    # them, because write_payload works on a copy and cannot see them.
    meta = ne_format.write_payload(outdir, columns, docs, vendor_names, vtok_bytes, meta)
    ne_format.write_token_blocks(outdir, dn_bytes, view_bytes, n)
    ne_format.write_desc_blocks(outdir, descriptions, n)
    ne_format.write_desc_sources(outdir, desc_sources, n)
    ne_format.write_desc_documents(outdir, desc_documents, n)
    ne_format.write_xdoc_blocks(outdir, xdoc_packed, n)
    meta["bytes"][ne_format.DESC_SRC] = os.path.getsize(
        os.path.join(outdir, ne_format.DESC_SRC))
    # ?selftest=1 is meant to cover everything the page loads, and this is now
    # one of those files.
    meta["digests"][ne_format.DESC_SRC] = zlib.crc32(
        open(os.path.join(outdir, ne_format.DESC_SRC), "rb").read())
    meta["bytes"][ne_format.DESC_DOC] = os.path.getsize(
        os.path.join(outdir, ne_format.DESC_DOC))
    meta["digests"][ne_format.DESC_DOC] = zlib.crc32(
        open(os.path.join(outdir, ne_format.DESC_DOC), "rb").read())
    write_search_index(outdir, descriptions, meta)
    write_vendor_groups(outdir, vendor_names, columns, meta)
    write_selftest(outdir, sources, columns["viewPresent"], n)
    ne_format.write_meta(outdir, meta)

    # A key added after write_payload and lost before the file was written is
    # invisible: the page just behaves as though the feature is not there.
    on_disk = json.load(open(os.path.join(outdir, ne_format.META), encoding="utf-8"))
    for key in ("wordCount", "vendorGroups", "descCount", "descSources", "bytes", "digests"):
        if key not in on_disk:
            sys.exit(f"meta.json is missing {key!r} — it was added after the file was written")
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"buildId": build_id, "dir": f"d/{build_id}"}, f)

    verify_payload(outdir, columns, docs, vendor_names, vtok_bytes,
                   dn_tokens, view_tokens, sources, descriptions)

    resident = sum(os.path.getsize(os.path.join(outdir, p)) for p in
                   (ne_format.META, ne_format.COLS_I32, ne_format.COLS_F64,
                    ne_format.COLS_U8, ne_format.DOCS, ne_format.VENDORS))
    tok = sum(os.path.getsize(ne_format.block_path(outdir, b))
              for b in range(ne_format.block_count(n))) + os.path.getsize(
                  os.path.join(outdir, ne_format.VTOK))
    desc = sum(os.path.getsize(ne_format.desc_path(outdir, b))
               for b in range(ne_format.block_count(n)))
    print(f"rows          : {n:,}")
    print(f"vendors       : {len(vendors):,}")
    print(f"source CSVs   : {orig / 1e6:6.2f} MB")
    print(f"resident      : {resident / 1e6:6.2f} MB raw  (loaded up front)")
    print(f"deferred      : {tok / 1e6:6.2f} MB raw in {ne_format.block_count(n):,} blocks "
          f"(fetched on click)")
    print(f"descriptions  : {desc / 1e6:6.2f} MB raw in {ne_format.block_count(n):,} blocks "
          f"({meta['descCount']:,} rows have one)")
    xdoc = sum(os.path.getsize(ne_format.xdoc_path(outdir, b))
               for b in range(ne_format.block_count(n)))
    print(f"document lists: {xdoc / 1e6:6.2f} MB raw in {ne_format.block_count(n):,} blocks "
          f"({meta['docMultiRows']:,} rows publish more than one)")
    print(f"written to    : {outdir}")


def add_descriptions_to_build():
    """Write description blocks into the build manifest.json already points at.

    Descriptions are purely additive -- new files, two new meta keys, and not
    one byte of any resident file -- so they can be attached to a payload that
    is already built, verified and live, instead of rebuilding it. That is not
    just a shortcut: it keeps the resident data byte-identical to what the
    browser selftest already passed on the CDN, and it means adding them costs
    no re-parse of 362 MB of CSV and no re-scrape.

    Everything needed is in the payload itself. Rows are keyed to
    scope.jsonl by view token, which is reconstructed from the token blocks
    exactly as extract_text.py addressed it when fetching the document.
    """
    with open(os.path.join(OUT_DIR, "manifest.json"), encoding="utf-8") as f:
        outdir = os.path.join(OUT_DIR, json.load(f)["dir"])
    print(f"attaching descriptions to {outdir}")

    columns, docs, vendors, _vtok, meta = ne_format.read_payload(outdir)
    n = meta["count"]
    _dn, view = ne_format.read_token_blocks(outdir, n)

    view_tokens = ["" for _ in range(n)]
    for i in range(n):
        if columns["viewPresent"][i]:
            view_tokens[i] = quote(base64.b64encode(view[i]).decode(), safe="")

    descriptions, desc_sources, desc_documents = load_descriptions(view_tokens, carry=False)
    if not descriptions:
        sys.exit(f"nothing to attach — {SCOPE_JSONL} is missing or matched no rows")

    ne_format.write_desc_blocks(outdir, descriptions, n)
    # Written here as well as in the full build, and that is the reason it is
    # its own file rather than a seventh u8 column: this path must never touch
    # cols.u8.bin, and a source column stranded there would say every row has
    # no description while the blocks beside it hold 540,000.
    ne_format.write_desc_sources(outdir, desc_sources, n)
    ne_format.write_desc_documents(outdir, desc_documents, n)
    meta.setdefault("bytes", {})[ne_format.DESC_SRC] = os.path.getsize(
        os.path.join(outdir, ne_format.DESC_SRC))
    meta.setdefault("digests", {})[ne_format.DESC_SRC] = zlib.crc32(
        open(os.path.join(outdir, ne_format.DESC_SRC), "rb").read())
    verify_descriptions(outdir, descriptions, n)

    meta["descCount"] = len(descriptions)
    meta["descBytes"] = sum(len(d) for d in descriptions.values())
    add_source_meta(meta, desc_sources)
    write_search_index(outdir, descriptions, meta)
    write_vendor_groups(outdir, vendors, columns, meta)
    with open(os.path.join(outdir, ne_format.META), "w", encoding="utf-8") as f:
        json.dump(meta, f, separators=(",", ":"), sort_keys=True)

    size = sum(os.path.getsize(ne_format.desc_path(outdir, b))
               for b in range(ne_format.block_count(n)))
    print(f"descriptions  : {size / 1e6:6.2f} MB raw in {ne_format.block_count(n):,} blocks "
          f"({meta['descCount']:,} of {n:,} rows)")
    print("resident files untouched — a page that has not been taught about "
          "descriptions reads this build exactly as before")


# What counts as a word. Runs of letters and digits, lowercased -- so
# "1/4 in. Square Head" indexes as 1, 4, in, square, head. Punctuation is a
# separator rather than content, which is what lets "CP3306-665" be found by
# searching either half. Purely numeric tokens are kept on purpose: 15% of the
# vocabulary, and a part number is exactly the kind of thing worth looking up.
WORD = re.compile(r"[a-z0-9]+")

# Below this a word is not worth an entry: single characters match so much that
# they are noise, and the page's other filters are better tools for them.
MIN_WORD = 2


def build_index(descriptions):
    """word -> sorted rows, over every description in this build."""
    postings = collections.defaultdict(list)
    for row in sorted(descriptions):
        # set(), so a description repeating a word does not repeat the row.
        for word in set(WORD.findall(descriptions[row].decode("utf-8").lower())):
            if len(word) >= MIN_WORD:
                postings[word].append(row)
    return postings


VENDOR_GROUPS_JSON = os.path.join(ROOT, "scripts", "vendor_groups.json")


def write_vendor_groups(outdir, vendor_names, columns, meta):
    """Attach the reviewed vendor groupings, and fail if any has gone stale.

    The mapping is written by hand (scripts/vendor_groups.json) from the
    spend-ranked candidates that scripts/suggest_vendor_groups.py proposes.
    Nothing is merged automatically: a wrong merge invents spending that never
    happened, which is worse than the fragmentation it would be fixing.

    A spelling listed there but absent from the data means the state has
    renamed or dropped that vendor since the review. Silently ignoring it would
    quietly shrink a published company total, so it stops the build instead --
    the same reasoning as the entity guard.
    """
    if not os.path.exists(VENDOR_GROUPS_JSON):
        meta["vendorGroups"] = []
        return

    with open(VENDOR_GROUPS_JSON, encoding="utf-8") as f:
        reviewed = {k: v for k, v in json.load(f).items() if not k.startswith("_")}

    index_of = {name.decode("utf-8"): i for i, name in enumerate(vendor_names)}
    canonical = sorted(reviewed)
    group_of_vendor = [-1] * len(vendor_names)

    missing = []
    for group, name in enumerate(canonical):
        for spelling in reviewed[name]:
            row = index_of.get(spelling)
            if row is None:
                missing.append((name, spelling))
                continue
            group_of_vendor[row] = group

    if missing:
        listed = "\n".join(f"    {name}: {spelling!r}" for name, spelling in missing[:10])
        sys.exit(f"{len(missing)} vendor spelling(s) in vendor_groups.json are not in the "
                 f"data:\n{listed}\n"
                 "The state has renamed or dropped them. Re-run "
                 "scripts/suggest_vendor_groups.py and update the file — leaving them "
                 "would quietly shrink a published company total.")

    ne_format.write_vendor_groups(outdir, group_of_vendor)

    decoded = ne_format.read_vendor_groups(outdir, len(vendor_names))
    if list(decoded) != group_of_vendor:
        sys.exit("vendor groups do not survive the round trip")

    meta["vendorGroups"] = canonical
    grouped = sum(1 for g in group_of_vendor if g >= 0)
    spend = collections.Counter()
    for row in range(meta["count"]):
        group = group_of_vendor[columns["vendorIdx"][row]]
        if group >= 0:
            spend[group] += columns["amount"][row]
    print(f"vendor groups : {len(canonical)} companies covering {grouped} spellings, "
          f"${sum(spend.values()) / 1e9:.2f} B")


def write_search_index(outdir, descriptions, meta):
    """Build the index, write it, and prove it decodes back to what went in.

    Verified the same way as everything else here -- read from disk rather than
    trusted -- because a subtly wrong index is worse than no index: it does not
    fail, it just quietly stops finding some contracts, and nobody can tell the
    difference between "no results" and "the index dropped them".
    """
    if not descriptions:
        meta["wordCount"] = 0
        return

    postings = build_index(descriptions)
    words = ne_format.write_index(outdir, postings)

    decoded = ne_format.read_index(outdir)
    if decoded != dict(postings):
        differing = [w for w in postings if decoded.get(w) != postings[w]]
        sys.exit(f"search index does not survive the round trip "
                 f"({len(differing):,} words differ, e.g. {differing[:3]})")

    meta["wordCount"] = len(words)
    size = (os.path.getsize(os.path.join(outdir, ne_format.WORDS))
            + os.path.getsize(os.path.join(outdir, ne_format.POSTINGS)))
    total = sum(len(v) for v in postings.values())
    print(f"search index  : {len(words):,} words, {total:,} postings, "
          f"{size / 1e6:.1f} MB raw — verified from disk")


def carry_descriptions_forward(view_tokens):
    """Row -> description bytes, recovered from the build being replaced.

    Descriptions come from a 20-hour document collection whose inputs
    (data/doc_text.jsonl, 1.3 GB) will never live in CI, so the weekly rebuild
    has no way to regenerate them. Without this it would publish a build with
    no descriptions at all and the feature would silently disappear every
    Sunday.

    They survive because they are keyed by view token, not by row: the token is
    the state's own identifier for a document and is stable across rebuilds,
    while row numbers are not -- a week of --daily scraping inserts records and
    shifts every row after them. So read the previous build's tokens and
    descriptions together, and re-key onto whatever rows this build has.

    Records added since the last extraction simply have none, which is honest:
    nobody has read their document yet.
    """
    manifest = os.path.join(OUT_DIR, "manifest.json")
    if not os.path.exists(manifest):
        return {}
    with open(manifest, encoding="utf-8") as f:
        previous = os.path.join(OUT_DIR, json.load(f)["dir"])
    if not os.path.isdir(previous):
        return {}

    columns, _docs, _vendors, _vtok, meta = ne_format.read_payload(previous)
    if not meta.get("descCount"):
        return {}

    count = meta["count"]
    _dn, view = ne_format.read_token_blocks(previous, count)
    previous_desc = ne_format.read_desc_blocks(previous, count)

    by_token = {}
    for i in range(count):
        if columns["viewPresent"][i] and previous_desc[i]:
            by_token[quote(base64.b64encode(view[i]).decode(), safe="")] = previous_desc[i]

    carried = {}
    for row, token in enumerate(view_tokens):
        text = by_token.get(token) if token else None
        if text:
            carried[row] = text

    print(f"descriptions  : {len(carried):,} carried forward from {os.path.basename(previous)}"
          + (f", {len(by_token) - len(carried):,} dropped (rows gone)"
             if len(by_token) > len(carried) else ""))
    return carried


# Which parser produced a description, as stored in descsrc.bin. This order is
# the on-disk encoding: append only, never renumber, or an old payload read by
# a newer page relabels every row.
#
# UNKNOWN exists for descriptions carried forward from a previous build, where
# the text survives and the source does not. That is not "no source" -- it is
# "we did not keep it", and the page says nothing rather than guessing.
DESC_SOURCES = ("", "line_items", "cover_sheet", "services_clause",
                "cover_sheet_form", "purchasing_bureau", "direct_purchase",
                "unknown", "contract_description")
DESC_SOURCE_CODE = {name: i for i, name in enumerate(DESC_SOURCES) if name}

# What the page prints above a description. Kept here rather than in index.html
# so that adding a parser needs no page change: the page reads the label from
# meta. A description reads very differently once you know it is a list of what
# was bought rather than a statement of what the contract is for.
DESC_SOURCE_LABELS = {
    "line_items": "Itemised on the purchase order",
    "cover_sheet": "Summary written on the University's contract cover sheet",
    "services_clause": "The contract's own scope-of-services clause",
    "cover_sheet_form": "The University cover sheet's description-of-purchase field",
    "purchasing_bureau": "The State Purchasing Bureau's description of the award",
    "direct_purchase": "The state's note that this purchase produced no contract",
    "contract_description": "The Department of Transportation's contract description and project location",
    "unknown": "",
}


def add_source_meta(meta, desc_sources):
    """The source dictionary and per-source counts the page renders from.

    The labels travel in the payload rather than living in index.html, so a
    build carrying a parser the page has never heard of still labels its rows,
    and an older payload read by a newer page labels its own the old way.
    """
    meta["descSources"] = [DESC_SOURCE_LABELS.get(name, "") for name in DESC_SOURCES]
    counts = collections.Counter(desc_sources.values())
    # Code 0 is every row the map says nothing about, which is most of the
    # point: 199,527 rows have no description, and a zero here would report the
    # opposite of the number a reader most wants.
    counts[0] = meta["count"] - sum(n for code, n in counts.items() if code)
    meta["descSourceCounts"] = [counts.get(code, 0) for code in range(len(DESC_SOURCES))]


def document_positions(view_tokens):
    """token -> (row, position in the state's list, is this the row's primary).

    Rows are identified by whichever of their documents the CSV's View URL
    points at, not by assuming that is the state's first: documents are not
    listed in date order, and a newly filed one can sort ahead of the one a
    row already carries. 197 rows had already moved by Aug 2026.

    Position is the state's own ordering, because that is what the page ships
    in its xdoc blocks and therefore what "the description came from this one"
    has to index into.
    """
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from scrape import load_documents  # noqa: E402

    row_of = {token: i for i, token in enumerate(view_tokens) if token}

    # Every row starts mapped by its own View URL, which is how descriptions
    # were keyed before records could have more than one document. This is not
    # a fallback for missing data -- documents.jsonl deliberately stores a
    # document list only for records publishing several, so the ~95% that
    # publish one appear nowhere below. Building the map from that file alone
    # dropped 532,720 rows' descriptions, and a local build hid it completely:
    # carry_descriptions_forward had them from the previous payload, so the
    # count looked right while the join underneath answered almost nothing. CI
    # has no previous build to carry from, and published 18,305 descriptions
    # where the site had 543,000.
    where = {token: (row, 0, True) for token, row in row_of.items()}

    for entry in load_documents(os.path.join(ROOT, "data", "documents.jsonl")).values():
        documents = entry.get("documents")
        if not documents:
            continue
        primary = next((d["token"] for d in documents if d["token"] in row_of), None)
        if primary is None:
            continue          # a record whose row this build does not carry
        row = row_of[primary]
        for position, d in enumerate(documents):
            where[d["token"]] = (row, position, d["token"] == primary)
    return where


def load_descriptions(view_tokens, carry=True):
    """(row -> description bytes, row -> source code, row -> document), keyed off tokens.

    scripts/extract_scope.py records the token it was fetched under, which is
    the same string the scrape found in the View URL -- an exact key, unlike
    document numbers, which repeat across document types. Rows the state
    publishes no file for simply have no entry.

    Optional: the descriptions come from a 20-hour document collection that
    nobody should have to run to build a payload. Without the file the build
    is exactly what it was before.
    """
    # The previous build is the floor, so a rebuild never loses descriptions it
    # was already publishing. A local scope.jsonl then overlays it, which is
    # strictly newer: it covers every document extracted so far, including any
    # collected since that build was made.
    descriptions = carry_descriptions_forward(view_tokens) if carry else {}
    # Everything carried forward arrived as text alone, so its source is not
    # knowable. Recorded as such, and overwritten below by anything scope.jsonl
    # also covers -- which, on a build with a current scope.jsonl, is all of it.
    sources = {row: DESC_SOURCE_CODE["unknown"] for row in descriptions}
    # Which of the record's documents a row's description was read from, as a
    # position in the state's list. Carried-forward text was joined by the
    # row's own View URL, so it came from the primary.
    documents = dict.fromkeys(descriptions, 0)

    if not os.path.exists(SCOPE_JSONL):
        if not descriptions:
            print(f"note: no {SCOPE_JSONL} and nothing to carry forward — "
                  "building without descriptions")
        return descriptions, sources, documents

    # A row is described by its primary if the primary says anything at all.
    # Only where it says nothing do the record's other documents get a turn,
    # in the state's order. Nothing already published is ever displaced: an
    # amendment's words are about the amendment, and swapping them in over a
    # contract's own description would change what the page says a record is
    # for without anyone asking for it.
    def rank(is_primary, position):
        return 0 if is_primary else 1 + position

    where = document_positions(view_tokens)
    best = {row: 0 for row in descriptions}
    added = filled = unmatched = 0
    unnamed = collections.Counter()
    with open(SCOPE_JSONL, encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            found = where.get(record["tok"])
            if found is None:
                unmatched += 1
                continue
            row, position, is_primary = found
            here = rank(is_primary, position)
            # Strictly better wins; equal rank lets scope.jsonl overlay what was
            # carried forward, which is the same document read more recently.
            if row in best and best[row] < here:
                continue
            if row not in descriptions:
                added += 1
                if not is_primary:
                    filled += 1
            descriptions[row] = record["description"].encode("utf-8")
            source = record.get("source") or ""
            if source and source not in DESC_SOURCE_CODE:
                # A parser this build has never heard of. Publishing it as 0
                # would say the row has no description while its text sits in
                # the block beside it, so it is named "unknown" and counted.
                unnamed[source] += 1
            sources[row] = DESC_SOURCE_CODE.get(source, DESC_SOURCE_CODE["unknown"])
            documents[row] = position
            best[row] = here

    print(f"descriptions  : {len(descriptions):,} joined to rows"
          + (f", {added:,} new since the last build" if added else "")
          + (f", {unmatched:,} unmatched" if unmatched else ""))
    if filled:
        print(f"                {filled:,} of those read from a document other than "
              "the record's first, where the first said nothing")
    for source, count in unnamed.most_common():
        print(f"    note: {count:,} rows carry source {source!r}, which this build "
              f"does not know — add it to DESC_SOURCES to label them")
    return descriptions, sources, documents


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


def verify_descriptions(outdir, descriptions, n):
    """Decode the description blocks and prove they carry the same text.

    Same rule as everything else here: read it back from disk rather than
    trusting the writer, so a wrong length header or a block boundary
    off by one fails now instead of showing the wrong contract's words
    next to a $38 million row.
    """
    decoded = ne_format.read_desc_blocks(outdir, n)
    for row in range(n):
        want = descriptions.get(row, b"")
        if decoded[row] != want:
            sys.exit(f"row {row}: description round trip failed\n"
                     f"  wrote {want[:80]!r}\n  read  {decoded[row][:80]!r}")
    present = sum(1 for d in decoded if d)

    # The two files are only useful together, and the failure that matters is
    # them disagreeing: a row the filter offers as having a description, whose
    # block is empty, or the reverse. Neither would raise anything on its own.
    codes = ne_format.read_desc_sources(outdir, n)
    if codes is None:
        sys.exit(f"{ne_format.DESC_SRC} was not written — the filter would report "
                 "that no row has a description")
    for row in range(n):
        if bool(decoded[row]) != bool(codes[row]):
            sys.exit(f"row {row}: description text and source disagree\n"
                     f"  text   {decoded[row][:60]!r}\n"
                     f"  source {codes[row]}")

    print(f"descriptions round-trip verified: {present:,} rows across "
          f"{ne_format.block_count(n):,} blocks, sources agree on every row")


def verify_payload(outdir, columns, docs, vendor_names, vtok_bytes,
                   dn_tokens, view_tokens, sources, descriptions=None):
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

    if descriptions is not None:
        verify_descriptions(outdir, descriptions, n)

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
