"""Split search.sqlite into fixed-size chunk files and write the config.json
sql.js-httpvfs needs to query it without downloading the whole thing.

This is a portable reimplementation of upstream's create_db.sh (GNU
`split --numeric-suffixes` and `stat --printf` aren't available in macOS's
BSD coreutils, which is what this project develops on).

Chunking -- rather than range-requesting a single file directly -- also
sidesteps a live, unresolved bug where GitHub Pages' CDN gzips .sqlite
files on HEAD requests but serves them uncompressed on ranged GETs, so the
auto-detected file length is wrong (github/pages-gem#952). serverMode
"chunked" never asks the server for the file's size at all: we already
know it exactly, since we just built the file.
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARCH_DIR = os.path.join(ROOT, "prototype-search")
IN_DB = os.path.join(SEARCH_DIR, "search.sqlite")
CHUNK_PREFIX = "search.sqlite."
CONFIG_JSON = os.path.join(SEARCH_DIR, "config.json")

# Must be a multiple of the database's page_size (see build_search_index.py).
SERVER_CHUNK_SIZE = 10 * 1024 * 1024
SUFFIX_LENGTH = 3

# Bytes fetched per HTTP range request. Doesn't need to match the database's
# actual page_size (1024, see build_search_index.py) -- larger values fetch
# more pages per request, trading wasted bandwidth for fewer round trips.
# Live testing on GitHub Pages showed cold queries are latency-bound (11-18s
# to fetch only 5-11MB), so cutting request count matters more than bytes
# here. 1024 was the httpvfs docs' example value, not a measured choice for
# this dataset -- 32x larger as a first experiment.
REQUEST_CHUNK_SIZE = 32 * 1024


def main():
    if not os.path.exists(IN_DB):
        raise SystemExit(f"{IN_DB} not found -- run build_search_index.py first")

    database_length_bytes = os.path.getsize(IN_DB)
    max_chunks = 10 ** SUFFIX_LENGTH
    n_chunks = -(-database_length_bytes // SERVER_CHUNK_SIZE)  # ceil
    if n_chunks > max_chunks:
        raise SystemExit(
            f"{n_chunks} chunks needed but suffix length {SUFFIX_LENGTH} only "
            f"allows {max_chunks} -- raise SERVER_CHUNK_SIZE or SUFFIX_LENGTH"
        )

    # Clear out any chunks from a previous build (a smaller rebuild could
    # otherwise leave stale trailing chunks behind).
    for name in os.listdir(SEARCH_DIR):
        if name.startswith(CHUNK_PREFIX):
            os.remove(os.path.join(SEARCH_DIR, name))

    with open(IN_DB, "rb") as f:
        for i in range(n_chunks):
            chunk = f.read(SERVER_CHUNK_SIZE)
            suffix = str(i).zfill(SUFFIX_LENGTH)
            with open(os.path.join(SEARCH_DIR, f"{CHUNK_PREFIX}{suffix}"), "wb") as out:
                out.write(chunk)

    config = {
        "serverMode": "chunked",
        "requestChunkSize": REQUEST_CHUNK_SIZE,
        "databaseLengthBytes": database_length_bytes,
        "serverChunkSize": SERVER_CHUNK_SIZE,
        "urlPrefix": CHUNK_PREFIX,
        "suffixLength": SUFFIX_LENGTH,
    }
    with open(CONFIG_JSON, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    print(f"Split {database_length_bytes:,} bytes into {n_chunks} chunk(s) of up to {SERVER_CHUNK_SIZE:,} bytes")
    print(f"Wrote {CONFIG_JSON}")


if __name__ == "__main__":
    main()
