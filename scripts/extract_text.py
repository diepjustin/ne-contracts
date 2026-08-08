"""Pull text out of the University of Nebraska contract/PO documents that are
born-digital PDFs.

A manual sample of 8 documents found ~40% have a real text layer and ~60%
are scanned images with none -- pypdf.extract_text() returns 0 characters
for the latter. This script only handles the text-native half; the scanned
half needs OCR, which is a separate (much heavier) job.

Each document's PDF is fetched and parsed in memory -- never written to
disk, since the full corpus is tens of gigabytes and we only want to keep
the text we can already read. Fetches run on a small thread pool (each
worker paced independently, like scripts/scrape.py's detail-page fetcher).

Results checkpoint to data/doc_text.jsonl as an append-only log, one JSON
object per line, keyed by the document's view token (stable and unique,
unlike document numbers which are not always unique across document
types). Appending is deliberate: with real text bodies included, the full
corpus's checkpoint is projected near 2 GB, and rewriting a file that size
from scratch on every checkpoint (the original design) gets slower as the
run progresses -- exactly the wrong direction for a job already expected to
take the better part of a day. Loading replays the log in order, so a
document processed twice (e.g. after a bug fix) just keeps its last entry.
"""

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pypdf import PdfReader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_JSON = os.path.join(ROOT, "data.json")
OUT_JSONL = os.path.join(ROOT, "data", "doc_text.jsonl")

# Below this average characters-per-page, treat the PDF as a scan with no
# text layer rather than real (if sparse) text. Measured empirically: real
# text averaged 1,700-2,900 chars/page across samples with a text layer;
# scanned pages measured exactly 0.
MIN_CHARS_PER_PAGE = 50

# Each worker paces its own requests this far apart. These are much heavier
# than the metadata scraper's detail-page fetches (documents average ~1.8
# MB), so both the delay and the worker count are more conservative than
# scrape.py's MAX_WORKERS=15 for lightweight HTML pages.
DOWNLOAD_DELAY = 0.5
MAX_WORKERS = 4

# How many documents to have in flight before checkpointing and re-checking
# the --hours deadline. Bounds memory/queue growth on a 30,000+ item run.
CHUNK_SIZE = 40


def build_session():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; research scraper)"})
    retry = Retry(
        total=5, connect=5, read=5, backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# One session per worker thread, so connection pools aren't shared across
# threads -- same pattern as detail_session() in scrape.py.
_local = threading.local()


def worker_session():
    if not hasattr(_local, "session"):
        _local.session = build_session()
    return _local.session


def load_checkpoint():
    store = {}
    if os.path.exists(OUT_JSONL):
        with open(OUT_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                store[rec["tok"]] = rec["data"]  # later lines win on a re-processed token
    return store


def append_checkpoint(new_items):
    """new_items: list of (token, result_dict) pairs to persist."""
    if not new_items:
        return
    os.makedirs(os.path.dirname(OUT_JSONL), exist_ok=True)
    with open(OUT_JSONL, "a", encoding="utf-8") as f:
        for tok, data in new_items:
            f.write(json.dumps({"tok": tok, "data": data}, ensure_ascii=False) + "\n")
        f.flush()


def extract(session, url):
    """Fetch one document and classify it. Returns a result dict.

    Pages are extracted individually and a page that raises (pypdf chokes on
    some malformed-but-common PDF constructs, e.g. an unresolved
    IndirectObject in a font's /Widths array) contributes empty text rather
    than failing the whole document -- a 40-page contract shouldn't lose
    every page's text because one page is odd.

    "unavailable" and "error" are kept separate on purpose: unavailable means
    the state doesn't have a file to serve here (a 404, or a 200 that isn't
    actually a PDF -- both observed on this site for other records), which is
    an expected, unfixable condition. "error" means something worth
    investigating or retrying later (network failure, or a PDF too malformed
    to even open). Conflating them would make a healthy long run's error
    count look alarming for no reason.
    """
    try:
        resp = session.get(url, timeout=60)
    except requests.RequestException as e:
        return {"status": "error", "detail": str(e)[:200]}

    if resp.status_code == 404:
        return {"status": "unavailable", "detail": "HTTP 404"}
    try:
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"status": "error", "detail": str(e)[:200]}

    if resp.content[:4] != b"%PDF":
        return {"status": "unavailable", "detail": f"non-PDF response ({resp.headers.get('Content-Type', '?')})"}

    try:
        reader = PdfReader(BytesIO(resp.content))
        page_count = len(reader.pages)
    except Exception as e:
        return {"status": "error", "detail": str(e)[:200]}

    texts = []
    failed_pages = 0
    for p in reader.pages:
        try:
            texts.append(p.extract_text() or "")
        except Exception:
            texts.append("")
            failed_pages += 1

    pages = page_count
    chars = sum(len(t) for t in texts)
    avg = chars / max(pages, 1)

    result_extra = {"failed_pages": failed_pages} if failed_pages else {}

    if avg < MIN_CHARS_PER_PAGE:
        return {"status": "scanned", "pages": pages, "chars": chars, **result_extra}

    return {"status": "text", "pages": pages, "chars": chars, "text": "\n\n".join(texts), **result_extra}


def extract_one(dn, tok, view_base):
    time.sleep(DOWNLOAD_DELAY)  # per-worker pacing, so concurrency doesn't remove politeness
    result = extract(worker_session(), view_base + tok)
    result["doc"] = dn
    return tok, result


def main():
    parser = argparse.ArgumentParser(description="Extract text from born-digital NU contract/PO PDFs.")
    parser.add_argument("--limit", type=int, default=None,
                         help="stop after this many NEW documents (for sampling before a full run)")
    parser.add_argument("--hours", type=float, default=None,
                         help="stop cleanly after this many hours, checkpointing as normal "
                              "(for running the full corpus in bounded, resumable chunks)")
    parser.add_argument("--status", action="store_true",
                         help="print progress counts and exit -- no network calls")
    args = parser.parse_args()

    with open(DATA_JSON, encoding="utf-8") as f:
        D = json.load(f)

    view_base = D["url"]["viewBase"]
    targets = [(row[0], row[9]) for row in D["rows"] if row[9]]  # (doc_number, view token)

    store = load_checkpoint()
    todo = [(dn, tok) for dn, tok in targets if tok not in store]

    if args.status:
        print(f"TOTAL={len(targets)}")
        print(f"DONE={len(store)}")
        print(f"REMAINING={len(todo)}")
        return

    print(f"{len(targets):,} documents have a view link, {len(store):,} already processed, {len(todo):,} remaining")

    if args.limit is not None:
        todo = todo[: args.limit]
        print(f"limiting this run to {len(todo):,} new documents")

    counts = {"text": 0, "scanned": 0, "unavailable": 0, "error": 0}
    start = time.time()
    deadline = start + args.hours * 3600 if args.hours else None
    processed = 0
    stopped_early = False

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for chunk_start in range(0, len(todo), CHUNK_SIZE):
            if deadline and time.time() >= deadline:
                print(f"\n{args.hours}h time limit reached after {processed:,} documents this run -- stopping cleanly.")
                stopped_early = True
                break

            chunk = todo[chunk_start: chunk_start + CHUNK_SIZE]
            futures = {pool.submit(extract_one, dn, tok, view_base): (dn, tok) for dn, tok in chunk}

            new_items = []
            for future in as_completed(futures):
                dn, tok = futures[future]
                try:
                    _, result = future.result()
                except Exception as e:
                    result = {"status": "error", "detail": f"worker crashed: {e}"[:200], "doc": dn}
                counts[result["status"]] += 1
                new_items.append((tok, result))

            append_checkpoint(new_items)
            processed += len(new_items)

            elapsed = time.time() - start
            rate = processed / elapsed if elapsed else 0
            done_so_far = min(chunk_start + CHUNK_SIZE, len(todo))
            print(f"  {done_so_far:,}/{len(todo):,}  text={counts['text']} scanned={counts['scanned']} "
                  f"unavailable={counts['unavailable']} error={counts['error']}  "
                  f"({rate:.2f}/s, {elapsed:.0f}s elapsed)")

    if not stopped_early:
        print(f"\nFinished this run's queue ({processed:,} documents).")

    total = len(store) + processed  # store wasn't updated in-place; this run's items are on disk, not re-read
    text_n = counts["text"]
    scanned_n = counts["scanned"]
    unavailable_n = counts["unavailable"]
    error_n = counts["error"]
    print(f"\nThis run: {processed:,} documents processed.")
    if processed:
        print(f"  text        : {text_n:,} ({text_n / processed * 100:.1f}%)")
        print(f"  scanned     : {scanned_n:,} ({scanned_n / processed * 100:.1f}%)")
        print(f"  unavailable : {unavailable_n:,} ({unavailable_n / processed * 100:.1f}%)  -- state has no file to serve")
        print(f"  error       : {error_n:,} ({error_n / processed * 100:.1f}%)  -- worth a retry pass")
    print(f"Cumulative total on disk: {total:,} documents.")


if __name__ == "__main__":
    main()
