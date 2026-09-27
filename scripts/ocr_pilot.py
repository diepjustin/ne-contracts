"""Is OCR worth running on the 108,000+ scanned documents at all?

scripts/extract_text.py already tells you which documents have no text layer
(status "scanned" in data/doc_text.jsonl) -- these are born-digital PDFs pypdf
and pdfminer both read as empty. The only way to reach them is OCR, which is a
separate and much heavier job, and README.md's "Open work" section scopes the
right first step before committing to it: run a small known sample through a
candidate engine and measure, on real documents, whether the result is worth
having -- not generic OCR accuracy, but whether extract_scope.describe() (the
same parser the whole site relies on) can find a description in what comes
back.

This pulls a fixed-seed sample of already-classified "scanned" documents,
fetches each one (politely, the same way scripts/extract_text.py does),
rasterizes its first few pages, runs one or more local OCR engines over them,
and scores the result with extract_scope.describe(). It reports, per engine:
wall-clock per page (so a full-corpus run can be estimated) and how often a
description was actually recovered (so cost isn't judged before quality is).

Two engines, both free and both local -- no cloud API here, on purpose: with
a spare machine available to run this unattended, dollars are not the
constraint, and a cloud engine is only worth adding later as a ceiling
reference if neither local engine reaches usable quality.

    tesseract   the open-source engine README.md's pilot already names.
                Needs the `tesseract` binary (e.g. `brew install tesseract`)
                and `pip install pytesseract`.
    vision      Apple's on-device Vision framework (what Live Text uses),
                accelerated by the Neural Engine on Apple Silicon. macOS only.
                `pip install ocrmac`.

Both also need `pip install pymupdf pillow` for PDF-to-image rendering.

Makes no attempt to touch data/doc_text.jsonl or data/scope.jsonl -- this is
an unverified experiment, not a second source of truth, and mixing OCR text
into the born-digital checkpoint would corrupt the one thing the rest of the
pipeline trusts. Results checkpoint to data/ocr_pilot.jsonl instead, same
append-and-skip-what's-done shape as extract_text.py, so an interrupted run
resumes rather than re-fetching.

Each engine's record keeps its OCR transcript in full (bounded by
--store-chars, same as extract_text.py's text capture), not just whether
describe() found something in it. A full backlog run costs real wall-clock
time -- re-running it just because a later pass wants the transcript itself,
rather than only the yes/no of whether describe() matched, would be wasteful.

    python3 scripts/ocr_pilot.py --status                 # progress, no network
    python3 scripts/ocr_pilot.py --engines tesseract       # any machine
    python3 scripts/ocr_pilot.py --engines vision          # macOS only
    python3 scripts/ocr_pilot.py --engines both            # macOS, side by side
    python3 scripts/ocr_pilot.py --report-only             # just re-print the summary
"""

import argparse
import collections
import json
import os
import random
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_scope  # noqa: E402 -- describe(), the score that actually matters
import extract_text  # noqa: E402 -- worker_session, load_checkpoint, the politeness gate
from build_site import VIEW_BASE  # noqa: E402 -- a plain constant, safe to import directly

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_JSONL = os.path.join(ROOT, "data", "ocr_pilot.jsonl")

# Fixed, so a re-run (or a run with a different --sample size that keeps this
# seed) draws from the same shuffle instead of a fresh, unreproducible sample.
SAMPLE_SEED = 20260915

DEFAULT_SAMPLE = 500

# Raised from 6/4000 on 2026-09-17: a corpus-wide check found 24,344
# documents (22.4%) actually exceeded the old page cap, most of them real
# multi-page contracts whose substantive terms -- scope of work, dollar
# figures -- sit past page 6 and were invisible to both the description
# parser and the AI summarizer. 200 pages / 100,000 characters comfortably
# covers every real contract observed in this corpus (the longest single
# agreement seen was 59 pages) while still bounding the ~522 documents that
# are actually bulk-scanned bundles rather than one document -- one ran to
# 3,003 pages. Those stop once they hit the character budget, typically well
# under 100 pages in, rather than running for the better part of an hour on
# text a one-sentence summary was never going to use anyway. See
# scripts/generate_ai_summaries.py's MAX_INPUT_CHARS, which is a separate,
# smaller cap on what actually reaches the model -- raising capture here
# doesn't by itself change what gets summarized.
DEFAULT_MAX_PAGES = 200
DEFAULT_DPI = 300
DEFAULT_STORE_CHARS = 100000

# extract_text.py's 12 workers were measured against a fetch-only workload;
# this one also spends CPU/Neural-Engine time OCR-ing every document it
# fetches, so a lower default avoids just building a backlog in memory. Raise
# it once you've watched one run and know the machine has headroom.
#
# Workers are separate processes, not threads, and that is load-bearing, not
# a style choice. ocrmac calls into Apple's Vision framework in-process via
# PyObjC, and measured on an M2 Pro across 8 identical pages: sequential
# 0.93s/image, 4 threads 0.78s/image (1.2x -- essentially nothing), 4
# processes 0.41s/image (2.3x). Threads never released the GIL around that
# call, so more of them just added scheduling overhead; separate processes
# each get their own interpreter and the speedup is real. Fetching also
# benefits -- a process blocked on network I/O no longer holds anything the
# next document's OCR needs.
DEFAULT_WORKERS = 4

# How many results to accumulate before writing and printing progress.
CHECKPOINT_EVERY = 20

ENGINE_CHOICES = ("tesseract", "vision", "both")


def load_scanned_sample(n, seed):
    """[(doc, tok, pages), ...] -- n documents drawn from the "scanned" ones
    extract_text.py has already found, so this never re-decides who needs OCR.
    """
    store = extract_text.load_checkpoint()
    scanned = [(data["doc"], tok, data["pages"])
               for tok, data in store.items() if data.get("status") == "scanned"]
    random.Random(SAMPLE_SEED if seed is None else seed).shuffle(scanned)
    return scanned, scanned[:n]


def load_pilot_checkpoint():
    """{tok: record}, same truncate-a-partial-final-line rule as
    extract_text.load_checkpoint -- this file is written the same way, by the
    same kind of job that gets killed mid-write.
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
        store[rec["tok"]] = rec
    return store


def append_pilot_checkpoint(records):
    if not records:
        return
    os.makedirs(os.path.dirname(OUT_JSONL), exist_ok=True)
    with open(OUT_JSONL, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


def check_engine_available(engine):
    """Fail before fetching anything, with a fix rather than a stack trace."""
    if engine == "tesseract":
        import shutil
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            raise SystemExit("--engines tesseract needs `pip install pytesseract pymupdf pillow`.")
        if shutil.which("tesseract") is None:
            raise SystemExit("--engines tesseract needs the `tesseract` binary on PATH -- "
                              "install it first, e.g. `brew install tesseract`.")
    elif engine == "vision":
        if sys.platform != "darwin":
            raise SystemExit("--engines vision calls Apple's Vision framework and only "
                              "runs on macOS. Use --engines tesseract on this machine.")
        try:
            import ocrmac  # noqa: F401
        except ImportError:
            raise SystemExit("--engines vision needs `pip install ocrmac pymupdf pillow`.")


def iter_page_images(pdf_bytes, max_pages, dpi):
    """Yields (image, render_seconds) pairs, one page at a time, first page
    first, up to max_pages.

    Lazy on purpose, and this is not a style preference: a 300 DPI page is
    roughly 24 MB as a raw RGB buffer, and this used to render every page up
    to max_pages -- all of them, unconditionally -- into one list before OCR
    got a chance to decide whether it needed them. That was harmless at
    max_pages=6 (six workers x six pages x 24 MB is still under a GB) and
    became a real, measured OOM risk once max_pages moved to 200 for the
    full-page recapture: a single 100-page document rendered eagerly is
    ~2.4 GB by itself, times up to `workers` documents in flight at once on
    a 16 GB machine, and it forced a full force-quit of the controlling
    session the first time this ran with the higher limit. store_chars is
    meant to be the actual bound on wasted work, but it could only stop OCR,
    never the rendering that had already happened before OCR ran at all.
    Rendering one page at a time and letting the caller decide, after each
    page, whether it has read enough closes that gap -- memory is now
    bounded by however many pages are in flight across all workers at once
    (effectively `workers`, not `workers * max_pages`), regardless of how
    long a document truly is or how high max_pages is set.

    The caller must call .close() on the returned generator once it stops
    consuming early (store_chars satisfied before max_pages), rather than
    relying on garbage collection, so the underlying fitz document -- and
    whatever page it's mid-render on -- releases promptly rather than
    whenever the collector next runs.
    """
    import fitz  # PyMuPDF
    from PIL import Image

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        zoom = dpi / 72
        matrix = fitz.Matrix(zoom, zoom)
        for page in doc[:max_pages]:
            t0 = time.time()
            pix = page.get_pixmap(matrix=matrix)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            yield image, time.time() - t0
    finally:
        doc.close()


def ocr_tesseract_page(image):
    import pytesseract
    return pytesseract.image_to_string(image)


def ocr_vision_page(image):
    from ocrmac import ocrmac

    annotations = ocrmac.OCR(image, recognition_level="accurate").recognize()
    # Vision's bounding boxes are normalized with the origin at bottom-left,
    # so descending y is top-to-bottom on the page; ascending x breaks ties
    # left-to-right. An approximation, not true multi-column reading order
    # -- but describe() needs one sentence intact, not a perfect transcript.
    ordered = sorted(annotations, key=lambda a: (-round(a[2][1], 2), a[2][0]))
    return "\n".join(a[0] for a in ordered)


ENGINE_PAGE_FUNCS = {"tesseract": ocr_tesseract_page, "vision": ocr_vision_page}


def fetch_pdf(session, tok):
    resp = session.get(VIEW_BASE + tok, timeout=60)
    resp.raise_for_status()
    if resp.content[:4] != b"%PDF":
        raise ValueError(f"non-PDF response ({resp.headers.get('Content-Type', '?')})")
    return resp.content


def process_one(dn, tok, pages, engines, max_pages, dpi, store_chars, delay):
    time.sleep(delay)
    record = {"tok": tok, "doc": dn, "pages": pages}

    t0 = time.time()
    try:
        pdf_bytes = fetch_pdf(extract_text.worker_session(), tok)
    except Exception as e:
        record["fetch_error"] = str(e)[:200]
        return record
    record["fetch_seconds"] = round(time.time() - t0, 2)

    # Render and OCR interleave page by page now, not render-everything-then-
    # OCR: see iter_page_images for why. `state` tracks each requested
    # engine's own accumulated text/chars/done-ness independently, so
    # --engines both still works -- a page keeps getting rendered and fed to
    # whichever engines haven't yet hit their own store_chars budget, and
    # rendering stops as soon as every engine has.
    try:
        page_iter = iter_page_images(pdf_bytes, max_pages, dpi)
    except Exception as e:
        record["render_error"] = str(e)[:200]
        return record

    pages_rendered = 0
    render_seconds = 0.0
    state = {e: {"texts": [], "chars": 0, "done": False, "seconds": 0.0, "error": None}
             for e in engines}
    try:
        for image, page_render_seconds in page_iter:
            if all(s["done"] for s in state.values()):
                break
            pages_rendered += 1
            render_seconds += page_render_seconds
            for engine, s in state.items():
                if s["done"]:
                    continue
                t0 = time.time()
                try:
                    text = ENGINE_PAGE_FUNCS[engine](image)
                except Exception as e:
                    s["error"] = str(e)[:200]
                    s["done"] = True
                    continue
                s["seconds"] += time.time() - t0
                s["texts"].append(text)
                s["chars"] += len(text)
                if store_chars and s["chars"] >= store_chars:
                    s["done"] = True
    except Exception as e:
        # A page mid-document failing to render (corrupt page, decode error)
        # is the same fact as the whole document failing to render -- there
        # is no partial-credit path elsewhere in this pipeline either.
        record["render_error"] = str(e)[:200]
        return record
    finally:
        # Not optional: an early stop (store_chars satisfied before
        # max_pages) leaves the generator suspended mid-iteration, and its
        # fitz document -- and whatever page it's rendering -- would
        # otherwise only release whenever the garbage collector next runs,
        # not when this function actually stops needing it.
        page_iter.close()

    record["render_seconds"] = round(render_seconds, 2)
    # What "rendered" means shifted slightly from before: it used to be
    # min(true page count, max_pages) unconditionally, because every page up
    # to max_pages was rendered regardless of whether OCR would ever read it.
    # Now it is however many pages actually got rendered before every engine
    # was satisfied -- usually smaller, and a more honest number, since a
    # page nobody's store_chars budget needed is a page that was never
    # rendered at all.
    record["pages_rendered"] = pages_rendered

    record["engines"] = {}
    for engine, s in state.items():
        if s["error"] and not s["texts"]:
            record["engines"][engine] = {"error": s["error"]}
            continue
        text = "\n\n".join(s["texts"])
        seconds = round(s["seconds"], 2)
        found = extract_scope.describe(text)
        record["engines"][engine] = {
            "seconds": seconds,
            "seconds_per_page": round(seconds / pages_rendered, 3) if pages_rendered else None,
            "chars": len(text),
            "described": found is not None,
            "source": found[0] if found else None,
            "description": found[1][:200] if found else None,
            # Kept in full (bounded by --store-chars, same cutoff each
            # engine's own per-page loop above stops accumulating at) so a
            # run doesn't have to be redone just because a later pass wants
            # the transcript itself -- e.g. to consider an AI-generated
            # description for documents describe() can't reach on its own.
            "text": text,
        }
    return record


def print_summary(checkpoint, total_scanned_docs, total_scanned_pages):
    records = list(checkpoint.values())
    attempted = len(records)
    fetch_errors = sum(1 for r in records if "fetch_error" in r)
    render_errors = sum(1 for r in records if "render_error" in r)
    usable = [r for r in records if "engines" in r]

    print(f"\n{attempted:,} documents attempted this pilot "
          f"({fetch_errors:,} fetch errors, {render_errors:,} render errors, "
          f"{len(usable):,} reached OCR).")
    if not usable:
        return

    by_engine = collections.defaultdict(list)
    for r in usable:
        for engine, result in r["engines"].items():
            if "error" not in result:
                by_engine[engine].append(result)

    print(f"\nFull backlog right now: {total_scanned_docs:,} scanned documents, "
          f"{total_scanned_pages:,} pages.\n")

    for engine, results in by_engine.items():
        n = len(results)
        described = sum(1 for r in results if r["described"])
        per_page = [r["seconds_per_page"] for r in results if r["seconds_per_page"]]
        avg_spp = sum(per_page) / len(per_page) if per_page else 0
        sources = collections.Counter(r["source"] for r in results if r["described"])

        print(f"-- {engine} --")
        print(f"  {n:,} documents OCR'd, {described:,} ({described / n * 100:.1f}%) "
              f"produced a description extract_scope.describe() could use")
        if sources:
            for source, count in sources.most_common():
                print(f"    {source:20}: {count:,}")
        if avg_spp:
            projected_hours = total_scanned_pages * avg_spp / 3600
            print(f"  {avg_spp:.2f}s/page -> ~{projected_hours:,.1f} core-hours "
                  f"projected for the full backlog (single-threaded; divide by "
                  f"however many workers actually run in parallel)")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Pilot OCR on a sample of already-known scanned documents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                         help=f"how many scanned documents to draw (default {DEFAULT_SAMPLE})")
    parser.add_argument("--seed", type=int, default=None,
                         help=f"shuffle seed (default {SAMPLE_SEED}, fixed for reproducibility)")
    parser.add_argument("--engines", choices=ENGINE_CHOICES, default="tesseract",
                         help="which OCR engine(s) to run (default tesseract; "
                              "'vision' and 'both' need macOS)")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                         help=f"rasterize at most this many pages per document "
                              f"(default {DEFAULT_MAX_PAGES})")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                         help=f"rasterization DPI (default {DEFAULT_DPI})")
    parser.add_argument("--store-chars", type=int, default=DEFAULT_STORE_CHARS,
                         help=f"stop OCR-ing a document's pages once this many characters "
                              f"are collected, 0 for no limit "
                              f"(default {DEFAULT_STORE_CHARS})")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                         help=f"concurrent fetch+OCR jobs (default {DEFAULT_WORKERS})")
    parser.add_argument("--delay", type=float, default=extract_text.DOWNLOAD_DELAY,
                         help=f"per-worker pause between fetches "
                              f"(default {extract_text.DOWNLOAD_DELAY}, matching extract_text.py)")
    parser.add_argument("--status", action="store_true",
                         help="print progress and exit -- no network calls")
    parser.add_argument("--report-only", action="store_true",
                         help="just re-print the summary from data/ocr_pilot.jsonl and exit")
    args = parser.parse_args()

    engines = ("tesseract", "vision") if args.engines == "both" else (args.engines,)

    all_scanned, sample = load_scanned_sample(args.sample, args.seed)
    total_scanned_pages = sum(pages for _, _, pages in all_scanned)
    checkpoint = load_pilot_checkpoint()

    if args.report_only:
        print_summary(checkpoint, len(all_scanned), total_scanned_pages)
        return

    todo = [(dn, tok, pages) for dn, tok, pages in sample if tok not in checkpoint]

    if args.status:
        print(f"TOTAL_SCANNED_CORPUS={len(all_scanned):,} documents, "
              f"{total_scanned_pages:,} pages")
        print(f"SAMPLE={len(sample):,}  DONE={len(sample) - len(todo):,}  "
              f"REMAINING={len(todo):,}")
        return

    for engine in engines:
        check_engine_available(engine)

    if todo and not extract_text.document_service_healthy():
        print("ERROR: refusing to run: the state is not serving documents right now, "
              "so every fetch would be recorded as an error for no reason. Nothing was "
              "written. Re-run once https://statecontracts.nebraska.gov is serving "
              "documents again.", file=sys.stderr)
        sys.exit(1)

    print(f"{len(sample):,} documents sampled, {len(sample) - len(todo):,} already done, "
          f"{len(todo):,} remaining this run. Engines: {', '.join(engines)}.")

    # A fixed chunk-of-CHECKPOINT_EVERY, submit-then-wait-for-all-20 loop is
    # fine when documents take roughly the same time each -- true of the
    # original short-document backlog (mostly <=6 pages), false of a
    # full-page recapture pass, whose queue is exactly the longest, most
    # variable documents in the corpus (a handful of pages next to a
    # 49-page contract next to a document that was actually a fetch error).
    # Submitting 20 at once and waiting for every one of them to finish
    # before submitting 20 more means a single slow straggler idles every
    # other worker once the other 19 are done -- measured on this exact
    # workload, a chunk delivering 257 real pages across 6 workers in 261s
    # implied only ~1 page/s of useful throughput, roughly 6-7x below what
    # 6 concurrently busy workers at the OCR engine's own measured per-page
    # rate should deliver. A rolling window -- always keep `workers` tasks
    # in flight, replace one the instant it finishes -- removes the
    # stall: an idle worker is handed the next queued document immediately,
    # regardless of what the slowest in-flight task is doing.
    start = time.time()
    processed = 0
    todo_iter = iter(todo)
    new_records = []

    def submit_next(pool):
        item = next(todo_iter, None)
        if item is None:
            return None
        dn, tok, pages = item
        return pool.submit(process_one, dn, tok, pages, engines,
                            args.max_pages, args.dpi, args.store_chars, args.delay)

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        in_flight = set()
        for _ in range(args.workers):
            fut = submit_next(pool)
            if fut is None:
                break
            in_flight.add(fut)

        while in_flight:
            done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    new_records.append(future.result())
                except Exception as e:
                    new_records.append({"crash": str(e)[:200]})
                processed += 1
                nxt = submit_next(pool)
                if nxt is not None:
                    in_flight.add(nxt)

            # Checkpoint roughly every CHECKPOINT_EVERY completions, same
            # cadence as before -- `done` can deliver more than one at a time
            # under this scheme, so this flushes on or shortly past the mark
            # rather than exactly at it, and always flushes whatever remains
            # once nothing is left in flight.
            if len(new_records) >= CHECKPOINT_EVERY or not in_flight:
                append_pilot_checkpoint(new_records)
                for rec in new_records:
                    checkpoint[rec.get("tok", id(rec))] = rec
                new_records = []

                elapsed = time.time() - start
                print(f"  {processed:,}/{len(todo):,}  "
                      f"({processed / elapsed:.2f} docs/s, {elapsed:.0f}s elapsed)")

    print_summary(checkpoint, len(all_scanned), total_scanned_pages)


if __name__ == "__main__":
    main()
