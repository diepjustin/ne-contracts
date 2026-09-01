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
import collections
import hashlib
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
# The ceiling turned out to be ours, not theirs, and contracts proved it:
# 10-page documents ran at 1.90/s where 1-page purchase orders did 8.1/s, on
# identical pacing. Twelve threads in pure-Python pypdf contend for the
# interpreter lock, so the work -- not the network -- was the limit.
#
# Not parsing pages whose text --store-chars will discard took that to 7.46/s,
# a 3.9x gain the server cannot even observe. That is the order to look for
# throughput in: cut our own work first, and only then consider asking a public
# records site for more.
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
    """Fold the log into {token: data}, last entry winning.

    A truncated final line is what stopping this job abruptly leaves behind, so
    it is removed rather than skipped: skipping is not enough, because the next
    run appends after the fragment and turns it into a broken line in the
    middle of the file, which would then fail every load. A broken line
    anywhere earlier is real corruption and is left to raise -- the entries
    after it cannot be trusted. Same rule as scrape.load_documents.
    """
    store = {}
    if not os.path.exists(OUT_JSONL):
        return store

    with open(OUT_JSONL, "rb") as f:
        raw = f.readlines()

    offset = 0
    for i, blob in enumerate(raw):
        start, offset = offset, offset + len(blob)
        line = blob.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            if i != len(raw) - 1:
                raise
            print(f"    Note: {OUT_JSONL} ends mid-write; truncating {len(blob)} bytes "
                  "of a partial line. That document is simply fetched again.")
            os.truncate(OUT_JSONL, start)
            break
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


def extract(session, url, store_chars=STORE_CHARS):
    """Fetch one document and classify it. Returns a result dict.

    Pages are extracted individually and a page that raises (pypdf chokes on
    some malformed-but-common PDF constructs, e.g. an unresolved
    IndirectObject in a font's /Widths array) contributes empty text rather
    than failing the whole document -- a 40-page contract shouldn't lose
    every page's text because one page is odd.

    Four outcomes, and which one a document gets decides whether it is ever
    asked about again:

      unavailable  a 404. The state has no file here. Never retried.
      unsupported  a 200 carrying a real file we cannot parse -- 17,991 TIFFs,
                   plus a handful of Word documents and JPEGs. Not retried
                   either, but it is not the same fact as "no file": these are
                   documents that exist and would answer to OCR.
      error        anything that might succeed on a second ask -- a network
                   failure, a PDF too malformed to open, or an HTML page where
                   a document should be.
      text/scanned a PDF we read.

    An HTML 200 used to be filed as "unavailable", on the reasoning that a 200
    which is not a PDF means the state has nothing here. It means no such
    thing. On 22 Aug 2026 every document request on the site returned this:

        An internal error occured: An error occurred within the Unity API:
        The type initializer for 'Hyland.Core.CoreUtility' threw an exception.

    -- a 200, an HTML body, and every document in the database still perfectly
    present. Recording that as "the state has no file" would have been a
    fabricated absence, permanent and unretried; a --retry-errors pass run
    during that window would have converted 14,586 recoverable documents into
    exactly that. HTML is now an error, which is retryable, and the body's hash
    and opening text are kept so an outage page can later be told apart from
    whatever else the state might serve.
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
        return classify_non_pdf(resp)

    try:
        reader = PdfReader(BytesIO(resp.content))
        page_count = len(reader.pages)
    except Exception as e:
        return with_pdfminer(resp.content, e, store_chars)

    # Stop once there is more text than --store-chars will keep. A contract
    # averages 9.9 pages and we keep the first ~1.7, so parsing the rest is
    # thrown away immediately -- and pypdf is pure Python, so on a 12-thread run
    # that wasted work is the actual ceiling: contracts managed 1.90/s where
    # purchase orders did 8.1/s. Cheaper for us and identical for the server,
    # which is the right order to look for throughput.
    #
    # The cutoff can only fire once `chars` has passed the limit, which means
    # text has already been found -- so a scan, whose pages yield nothing, is
    # never cut short and never misclassified by it.
    texts = []
    failed_pages = 0
    parsed_pages = 0
    chars = 0
    for p in reader.pages:
        parsed_pages += 1
        try:
            text = p.extract_text() or ""
        except Exception:
            text = ""
            failed_pages += 1
        texts.append(text)
        chars += len(text)
        if store_chars and chars >= store_chars:
            break

    pages = page_count
    # Averaged over the pages actually read, not the whole document: dividing
    # text from two pages by ten pages' worth would read as a scan.
    avg = chars / max(parsed_pages, 1)

    # Every page raising is not a scan, it is a parse failure wearing a scan's
    # clothes -- and "scanned" is not an error, so it would never be retried.
    if failed_pages and failed_pages == pages:
        return with_pdfminer(resp.content, f"pypdf failed on all {pages} pages", store_chars)

    result_extra = {"failed_pages": failed_pages} if failed_pages else {}
    if parsed_pages < pages:
        # `chars` counts only what was read; `pages` is still the real total.
        result_extra["parsed_pages"] = parsed_pages

    if avg < MIN_CHARS_PER_PAGE:
        return {"status": "scanned", "pages": pages, "chars": chars, **result_extra}

    return {"status": "text", "pages": pages, "chars": chars, "text": "\n\n".join(texts), **result_extra}


# Content types that are a document the state really does hold, just not one
# pypdf can open. Counted across the 32,227 non-PDF responses on disk: 17,991
# image/tiff, 34 .docx, 7 image/jpeg, 7 application/msword. Every one of those
# is a scan or a file awaiting OCR, and calling them "unavailable" says the
# state published nothing when it published something we cannot read.
FILE_TYPES = ("image/", "application/msword", "application/vnd.openxmlformats",
              "application/vnd.ms-", "application/rtf", "text/rtf")

# Kept on any non-PDF answer, so the next person can tell an outage page from a
# real one without re-fetching 14,000 documents. A hash collapses "they all
# served the identical error" into one line; the opening text says which error.
BODY_HEAD_CHARS = 300


def classify_non_pdf(resp):
    """A 200 that is not a PDF: a file we cannot read, or not a file at all."""
    content_type = (resp.headers.get("Content-Type") or "?").lower()
    record = {
        "detail": f"non-PDF response ({content_type})",
        "contentType": content_type,
        "bodySha256": hashlib.sha256(resp.content).hexdigest(),
    }
    if any(content_type.startswith(t) for t in FILE_TYPES):
        # A real file. Not retryable -- asking again returns the same TIFF --
        # but not an absence either.
        return {"status": "unsupported", **record}

    # HTML, or anything else that is not a document. This is what the state
    # serves while its document service is down, so it must stay retryable.
    record["bodyHead"] = " ".join(
        resp.text[:2000].split())[:BODY_HEAD_CHARS] if resp.text else ""
    return {"status": "error", **record}


def with_pdfminer(content, pypdf_error, store_chars=STORE_CHARS):
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
    result = extract(worker_session(), view_base + tok, store_chars)
    result["doc"] = dn
    text = result.get("text")
    if text is not None and store_chars and len(text) > store_chars:
        # `chars` still reports the whole document -- only what we keep is cut,
        # and `clipped` says so, so a prefix is never mistaken for the full text.
        result["text"] = text[:store_chars]
        result["clipped"] = True
    return tok, result


def extra_document_targets():
    """(view_base, [(document number, view token), ...]) for the *other* documents.

    load_targets below returns one token per row -- the record's primary
    document, which is all the scraper used to capture. Since Aug 2026
    data/documents.jsonl holds every document each record publishes, and
    37,596 records publish more than one: 73,690 documents nobody has ever
    read, sitting behind 22,342 rows the site shows no description for.

    Ordered so rows with no description come first. This fetch is expected to
    be stopped, and that way stopping early has already bought the descriptions
    that were missing rather than re-reading records that already have one.

    Primaries are excluded: load_targets already covers them, and
    doc_text.jsonl would skip them on its checkpoint regardless.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from scrape import load_documents  # noqa: E402

    with open(os.path.join(ROOT, "manifest.json"), encoding="utf-8") as f:
        outdir = os.path.join(ROOT, json.load(f)["dir"])
    with open(os.path.join(outdir, "meta.json"), encoding="utf-8") as f:
        view_base = json.load(f)["viewBase"]

    described = set()
    scope = os.path.join(ROOT, "data", "scope.jsonl")
    if os.path.exists(scope):
        with open(scope, encoding="utf-8") as f:
            for line in f:
                try:
                    described.add(json.loads(line)["tok"])
                except (json.JSONDecodeError, KeyError):
                    continue

    blank, already = [], []
    for entry in load_documents(os.path.join(ROOT, "data", "documents.jsonl")).values():
        documents = entry.get("documents")
        if not documents or len(documents) < 2:
            continue
        extras = [(entry["doc"], d["token"]) for d in documents[1:]]
        (already if documents[0]["token"] in described else blank).extend(extras)

    print(f"{len(blank):,} documents behind rows with no description, "
          f"{len(already):,} behind rows that already have one")
    return view_base, blank + already


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


def never_proven_absent(record):
    """True for a stored "unavailable" that no 404 ever justified.

    32,224 records on disk say "unavailable" because a 200 came back carrying
    something other than a PDF, under the old reading that this meant the state
    had no file. Two different things are sitting in there:

      * 17,991 TIFFs and a few Word documents -- files that exist and are now
        classified "unsupported". They will come back the same way; the point
        of re-asking is to stop the corpus calling them missing.
      * 14,185 HTML answers, which cannot be told apart from the error page the
        state served all day on 22 Aug 2026, because no body was kept. Any of
        them may be a document that was simply unreachable that hour.

    A 404 is left alone. That one was an answer.
    """
    if record.get("status") != "unavailable":
        return False
    return "404" not in (record.get("detail") or "")


def document_service_healthy():
    """scrape.py's canary, borrowed rather than copied.

    Imported the same lazy way entity_filter() takes its entity lists. Two
    canaries kept in step by hand would drift, and the one that mattered would
    be the one that had.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from scrape import document_service_healthy as healthy  # noqa: E402

    return healthy()


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
    parser.add_argument("--extras", action="store_true",
                        help="read the other documents a record publishes, not its "
                             "first -- 73,690 of them, blank rows first")
    parser.add_argument("--retry-errors", action="store_true",
                         help="also re-fetch documents whose last attempt errored; without "
                              "this they count as done and are skipped forever")
    parser.add_argument("--retry-non-pdf", action="store_true",
                         help="also re-fetch the 32,224 documents recorded as unavailable "
                              "because a 200 returned something other than a PDF. A 404 is "
                              "left alone. Sorts the TIFFs (real files, now 'unsupported') "
                              "from the HTML answers, which may only have been an outage")
    args = parser.parse_args()

    if args.extras:
        view_base, targets = extra_document_targets()
    else:
        view_base, targets = load_targets(args.group, entity_filter(args.entities))

    store = load_checkpoint()
    # A recorded error counts as done, which is right for a resumable run and
    # wrong forever after: network blips and malformed PDFs would never be
    # retried. A 404 stays excluded -- the state has no file for those, and
    # re-asking will not change that.
    todo = [(dn, tok) for dn, tok in targets
            if tok not in store
            or (args.retry_errors and store[tok].get("status") == "error")
            or (args.retry_non_pdf and never_proven_absent(store[tok]))]
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

    # The same gate scrape.py puts in front of --daily and a full sweep, and it
    # belongs here for the same reason: this script's failure statuses are
    # written to an append-only log and two of them are never asked about
    # again. During the 22 Aug 2026 outage every fetch returned an error page,
    # so a run started that morning would have recorded a permanent absence for
    # every document it touched -- and with --retry-errors it would have spent
    # those verdicts on the 14,586 documents most worth recovering.
    #
    # Checked here rather than in fetch_and_extract(): one request before the
    # run, not one per document, and nothing is written before it answers.
    if todo and not document_service_healthy():
        print("ERROR: refusing to run: the state is not serving documents right now, so "
              "every document fetched would be recorded as one it does not have. Nothing "
              "was written. Re-run once https://statecontracts.nebraska.gov is serving "
              "documents again.", file=sys.stderr)
        sys.exit(1)

    counts = collections.Counter()
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
                # Only classify_non_pdf() sets this, and only when the answer
                # was not a file of any kind -- which is what an outage looks
                # like from here.
                if "bodyHead" in result:
                    counts["not_a_file"] += 1
                new_items.append((tok, result))

            append_checkpoint(new_items)
            processed += len(new_items)

            elapsed = time.time() - start
            rate = processed / elapsed if elapsed else 0
            done_so_far = min(chunk_start + CHUNK_SIZE, len(todo))
            print(f"  {done_so_far:,}/{len(todo):,}  text={counts['text']} scanned={counts['scanned']} "
                  f"unavailable={counts['unavailable']} unsupported={counts['unsupported']} "
                  f"error={counts['error']}  ({rate:.2f}/s, {elapsed:.0f}s elapsed)")

    if not stopped_early:
        print(f"\nFinished this run's queue ({processed:,} documents).")

    total = len(store) + processed  # store wasn't updated in-place; this run's items are on disk, not re-read
    print(f"\nThis run: {processed:,} documents processed.")
    if processed:
        for status, note in (
                ("text", ""),
                ("scanned", "-- a PDF with no text layer; only OCR reaches these"),
                ("unavailable", "-- 404, the state has no file to serve"),
                ("unsupported", "-- a real file we cannot parse, mostly TIFF scans"),
                ("error", "-- worth a retry pass"),
        ):
            n = counts[status]
            print(f"  {status:12}: {n:,} ({n / processed * 100:.1f}%)  {note}".rstrip())
        # An error rate this high is not a bad batch of PDFs. It is the symptom
        # the health gate exists to catch mid-run, after it has already passed
        # once at the start.
        not_a_file = counts["not_a_file"]
        if not_a_file > processed * 0.5:
            print(f"\n  WARNING: {not_a_file:,} of {processed:,} documents answered with "
                  f"something that was not a file at all. Check that the state is still "
                  f"serving documents before trusting this run.")
    print(f"Cumulative total on disk: {total:,} documents.")


if __name__ == "__main__":
    main()
