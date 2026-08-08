"""Build a SQLite FTS4 database over the extracted document text, for
full-text search on the text-native subset that scripts/extract_text.py
has processed.

Built for sql.js-httpvfs: page_size=1024 (matches PAGE_SIZE below, used as
requestChunkSize by the browser) and journal_mode=delete, both required by
httpvfs's README, then VACUUM to actually apply the page size to an
existing file. See prototype-search/index.html for the page that queries
it, and scripts/chunk_search_db.py for the step that splits this file into
range-request-friendly chunks.
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_text import load_checkpoint  # noqa: E402 -- shared loader, format is its concern not ours

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_JSON = os.path.join(ROOT, "data.json")
OUT_DB = os.path.join(ROOT, "prototype-search", "search.sqlite")

# Trade-off per the httpvfs docs: smaller pages mean more, smaller HTTP
# requests per query but less wasted bandwidth per request. Must match
# requestChunkSize in the config scripts/chunk_search_db.py writes.
PAGE_SIZE = 1024


def fmt_date(n):
    if not n:
        return ""
    s = str(n)
    return f"{s[4:6]}/{s[6:8]}/{s[0:4]}"


def main():
    with open(DATA_JSON, encoding="utf-8") as f:
        D = json.load(f)
    doc_text = load_checkpoint()

    view_base = D["url"]["viewBase"]
    vendors = D["vendors"]
    entities = D["meta"]["entities"]
    types = D["meta"]["types"]
    statuses = D["meta"]["statuses"]

    os.makedirs(os.path.dirname(OUT_DB), exist_ok=True)
    if os.path.exists(OUT_DB):
        os.remove(OUT_DB)

    con = sqlite3.connect(OUT_DB)
    # journal_mode must be non-WAL before page_size takes effect, per the
    # httpvfs README -- both have to be set before any tables are created.
    con.execute("PRAGMA journal_mode=delete")
    con.execute(f"PRAGMA page_size={PAGE_SIZE}")
    # sql.js's stock WASM build compiles in FTS3/4 but not FTS5, so the
    # index targets FTS4 -- notindexed= is its equivalent of FTS5's
    # UNINDEXED column modifier.
    con.execute(
        """
        CREATE VIRTUAL TABLE documents USING fts4(
            doc_number,
            vendor,
            entity,
            doc_type,
            status,
            amount,
            begin_date,
            end_date,
            view_url,
            text,
            notindexed=doc_number,
            notindexed=entity,
            notindexed=doc_type,
            notindexed=status,
            notindexed=amount,
            notindexed=begin_date,
            notindexed=end_date,
            notindexed=view_url,
            tokenize=porter
        )
        """
    )

    n = 0
    seen_tokens = set()
    for row in D["rows"]:
        tok = row[9]
        if not tok or tok not in doc_text:
            continue
        rec = doc_text[tok]
        if rec["status"] != "text":
            continue
        if tok in seen_tokens:
            # A handful of rows (e.g. a Contract and its matching Purchase
            # Order) point at the identical scanned file on the state's
            # site -- index it once, not once per row that references it.
            continue
        seen_tokens.add(tok)

        con.execute(
            "INSERT INTO documents (doc_number, vendor, entity, doc_type, status, amount, "
            "begin_date, end_date, view_url, text) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row[0],
                vendors[row[1]],
                entities[row[6]],
                types[row[7]],
                statuses[row[5]],
                row[2],
                fmt_date(row[3]),
                fmt_date(row[4]),
                view_base + tok,
                rec["text"],
            ),
        )
        n += 1

    con.commit()
    con.execute("INSERT INTO documents(documents) VALUES('optimize')")
    con.commit()
    con.execute("VACUUM")  # rewrites the file, actually applying page_size
    con.close()

    size = os.path.getsize(OUT_DB)
    print(f"Indexed {n:,} documents into {OUT_DB} ({size / 1e6:.2f} MB, page_size={PAGE_SIZE})")
    print("Run scripts/chunk_search_db.py next to prep it for httpvfs.")


if __name__ == "__main__":
    main()
