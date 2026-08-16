"""Pull text out of the published documents that are born-digital PDFs.

How much of the corpus that covers depends entirely on the document type,
and the difference is large enough to plan around. Measured across the
31,042 documents captured so far:

    purchase orders   98.4% have a text layer
    contracts         38.7%

An early 8-document sample suggested ~40% overall and that number is still
quoted in places; it was drawn from contracts alone and is badly wrong for
the corpus as a whole, which is 84% purchase orders. The scanned remainder
needs OCR, which is a separate and much heavier job.

What is worth reading these for is the description -- the state publishes
no such field, but most documents contain one written by whoever filed
them. See scripts/extract_scope.py, which parses whatever this collects.

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
import base64
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pypdf import PdfReader
from pdfminer.high_level import extract_text as pdfminer_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_JSONL = os.path.join(ROOT, "data", "doc_text.jsonl")

# How much of each document's text to keep. The descriptions this feeds live
# on the first page, and keeping every document whole does not scale: the
# 31,042 captured so far are already 408 MB, which extrapolates to roughly
# 9 GB across all 691,145. A prefix keeps re-parsing possible -- improve a
# pattern, re-run extract_scope.py, no re-downloading -- without that.
STORE_CHARS = 4000

# Below this average characters-per-page, treat the PDF as a scan with no
# text layer rather than real (if sparse) text. Measured empirically: real
# text averaged 1,700-2,900 chars/page across samples with a text layer;
# scanned pages measured exactly 0.
MIN_CHARS_PER_PAGE = 50

# Each worker paces its own requests this far apart.
#
# 12 workers is measured, not guessed, and it is worth knowing that the
# metadata scraper's finding does NOT transfer here. There, 15 -> 20 workers
# bought identical wall time and only raised latency, because search results
# live in server-side session state that serializes. This endpoint just serves
# a PDF blob, and it scales nearly linearly to 12:
#
#     workers    rate      vs 4
#      4 (old)   3.05/s    1.0x     0 errors
#      8         5.92/s    1.9x     0 errors
#     12         9.09/s    3.0x     2 errors per 300
#     16         8.29/s    2.7x     2 errors per 300   <- slower than 12
#
# 16 being slower than 12 is the same queuing signature, three times further
# out. Do not push past 12: it is measurably unproductive, and at ~32,000
# requests an hour this is already assertive for a public records server.
#
# Whether the ceiling is theirs or ours is untested -- pypdf is pure Python,
# so twelve threads parsing PDFs contend for the interpreter lock, and the
# flattening may be this machine. If more throughput is ever needed, cut our
# own CPU first (stop parsing pages past --store-chars) rather than asking
# the server for more.
DOWNLOAD_DELAY = 0.5
MAX_WORKERS = 12

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
        return with_pdfminer(resp.content, e)

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

    # Every page raising is not a scan, it is a parse failure wearing a scan's
    # clothes -- and "scanned" is not an error, so it would never be retried.
    if failed_pages and failed_pages == pages:
        return with_pdfminer(resp.content, f"pypdf failed on all {pages} pages")

    result_extra = {"failed_pages": failed_pages} if failed_pages else {}

    if avg < MIN_CHARS_PER_PAGE:
        return {"status": "scanned", "pages": pages, "chars": chars, **result_extra}

    return {"status": "text", "pages": pages, "chars": chars, "text": "\n\n".join(texts), **result_extra}


def with_pdfminer(content, pypdf_error):
    """Second opinion on a PDF pypdf could not read.

    pypdf is strict about structure and gives up on documents that render
    perfectly well: 3,181 of the corpus's failures are a single "Invalid object
    in /Pages". pdfminer reconstructs more and opened 12 of 12 of those in a
    sample, so it is worth the second attempt before calling a document lost.

    Only reached when pypdf has already failed, so nothing that currently works
    changes path. The original pypdf error is kept in the record -- if this ever
    needs debugging, "which parser, failing how" is the question.
    """
    try:
        text = pdfminer_text(BytesIO(content))
    except Exception as e:
        return {"status": "error",
                "detail": f"pypdf: {str(pypdf_error)[:90]} | pdfminer: {str(e)[:90]}"}

    # pdfminer separates pages with a form feed, so the count comes free.
    pages = text.count("\f") or 1
    chars = len(text)
    if chars / pages < MIN_CHARS_PER_PAGE:
        return {"status": "scanned", "pages": pages, "chars": chars, "parser": "pdfminer"}
    return {"status": "text", "pages": pages, "chars": chars, "text": text,
            "parser": "pdfminer"}


def extract_one(dn, tok, view_base, store_chars, delay=None):
    # Per-worker pacing, so concurrency doesn't remove politeness.
    time.sleep(DOWNLOAD_DELAY if delay is None else delay)
    result = extract(worker_session(), view_base + tok)
    result["doc"] = dn
    text = result.get("text")
    if text is not None and store_chars and len(text) > store_chars:
        # `chars` still reports the whole document -- only what we keep is cut,
        # and `clipped` says so, so a prefix is never mistaken for the full text.
        result["text"] = text[:store_chars]
        result["clipped"] = True
    return tok, result


def load_targets(group=None, entities=None):
    """(view_base, [(document number, view token), ...]) for documents with a file.

    Reads the published payload via manifest.json. It used to read data.json,
    which is the 295,895-row corpus this project outgrew -- so it could not see
    the 442,300 records scraped since, which is nearly every purchase order and
    every state agency document. Anything reading data.json is reading history.

    `group` filters to "Contract" or "Purchase Order"; `entities` to a set of
    entity names. Both matter because the document types differ so much: a
    purchase order is a one-page form that almost always has a text layer, a
    contract is a scanned agreement six times out of ten.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ne_format  # noqa: E402

    with open(os.path.join(ROOT, "manifest.json"), encoding="utf-8") as f:
        outdir = os.path.join(ROOT, json.load(f)["dir"])

    columns, docs, _vendors, _vtokens, meta = ne_format.read_payload(outdir)
    _dn, view = ne_format.read_token_blocks(outdir, meta["count"])

    targets = []
    for i in range(meta["count"]):
        if not columns["viewPresent"][i]:
            continue  # the state has no file to serve for this row
        if group is not None and meta["typeGroups"][columns["type"][i]] != group:
            continue
        if entities is not None and meta["entities"][columns["entity"][i]] not in entities:
            continue
        # Tokens are stored decoded; the URL wants them as the scrape found them.
        token = quote(base64.b64encode(view[i]).decode(), safe="")
        targets.append((docs[i].decode("utf-8"), token))

    return meta["viewBase"], targets


def entity_filter(which):
    """The set of entity names for --entities, or None for all 92."""
    if which == "all":
        return None
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from scrape import HIGHER_ED_ENTITIES, STATE_ENTITIES  # noqa: E402

    return set(STATE_ENTITIES if which == "state" else HIGHER_ED_ENTITIES)


def main():
    parser = argparse.ArgumentParser(description="Extract text from born-digital NU contract/PO PDFs.")
    parser.add_argument("--limit", type=int, default=None,
                         help="stop after this many NEW documents (for sampling before a full run)")
    parser.add_argument("--hours", type=float, default=None,
                         help="stop cleanly after this many hours, checkpointing as normal "
                              "(for running the full corpus in bounded, resumable chunks)")
    parser.add_argument("--status", action="store_true",
                         help="print progress counts and exit -- no network calls")
    parser.add_argument("--group", choices=("Contract", "Purchase Order"), default=None,
                         help="only this document category (they behave very differently)")
    parser.add_argument("--entities", choices=("all", "state", "higher-ed"), default="all",
                         help="only these entities (default: all 92)")
    parser.add_argument("--store-chars", type=int, default=STORE_CHARS, metavar="N",
                         help=f"keep only the first N characters of each document, "
                              f"0 for all of it (default {STORE_CHARS})")
    parser.add_argument("--shuffle", action="store_true",
                         help="draw in a fixed random order -- use with --limit, since "
                              "documents sit in entity order and a contiguous slice "
                              "samples one agency rather than the corpus")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, metavar="N",
                         help=f"concurrent fetches (default {MAX_WORKERS}); measure before "
                              f"raising -- on the metadata scraper more workers bought "
                              f"nothing and only raised latency")
    parser.add_argument("--delay", type=float, default=DOWNLOAD_DELAY, metavar="SECONDS",
                         help=f"per-worker pause between fetches (default {DOWNLOAD_DELAY})")
    parser.add_argument("--retry-errors", action="store_true",
                         help="also re-fetch documents whose last attempt errored; without "
                              "this they count as done and are skipped forever")
    args = parser.parse_args()

    view_base, targets = load_targets(args.group, entity_filter(args.entities))

    store = load_checkpoint()
    # A recorded error counts as done, which is right for a resumable run and
    # wrong forever after: network blips and malformed PDFs would never be
    # retried. "unavailable" is deliberately not included -- the state has no
    # file to serve for those, and re-asking will not change that.
    todo = [(dn, tok) for dn, tok in targets
            if tok not in store
            or (args.retry_errors and store[tok].get("status") == "error")]
    # Against the selected targets, not the whole checkpoint: with a --group or
    # --entities filter those differ, and reporting the checkpoint total would
    # claim work was done on documents this run has not looked at.
    done = len(targets) - len(todo)

    if args.status:
        print(f"TOTAL={len(targets)}")
        print(f"DONE={done}")
        print(f"REMAINING={len(todo)}")
        return

    print(f"{len(targets):,} documents have a view link, {done:,} already processed, {len(todo):,} remaining")

    if args.shuffle:
        # Seeded, so a re-run picks up where the last one left off instead of
        # re-drawing a fresh sample and re-fetching documents already held.
        random.Random(20260814).shuffle(todo)

    if args.limit is not None:
        todo = todo[: args.limit]
        print(f"limiting this run to {len(todo):,} new documents"
              + (" (random order)" if args.shuffle else ""))

    counts = {"text": 0, "scanned": 0, "unavailable": 0, "error": 0}
    start = time.time()
    deadline = start + args.hours * 3600 if args.hours else None
    processed = 0
    stopped_early = False

    print(f"pacing: {args.workers} workers, {args.delay}s per-worker delay")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for chunk_start in range(0, len(todo), CHUNK_SIZE):
            if deadline and time.time() >= deadline:
                print(f"\n{args.hours}h time limit reached after {processed:,} documents this run -- stopping cleanly.")
                stopped_early = True
                break

            chunk = todo[chunk_start: chunk_start + CHUNK_SIZE]
            futures = {pool.submit(extract_one, dn, tok, view_base, args.store_chars,
                                   args.delay): (dn, tok)
                       for dn, tok in chunk}

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
