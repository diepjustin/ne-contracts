"""Generate a plain-English AI summary for every OCR'd document.

Unlike extract_scope.py, this is deliberately generative: it asks a local
LLM to describe what a document is and what it's for, rather than lifting
the state's own words verbatim. That is a different kind of claim than
everything else this project publishes, and it is only defensible because
of how it is presented -- every summary this script produces is meant to
ship labeled as AI-generated and unverified, a pointer toward the source
document rather than a fact to cite. There is no verification pass here
(that is the whole reason this exists: OCR'd documents are the ones nobody
has read), so nothing downstream should treat this output as ground truth.

Runs entirely against data/ocr_pilot.jsonl's already-captured OCR text --
no network fetch, no re-OCR. That file's "text" field exists specifically so
a pass like this one never has to re-run the (expensive) OCR step just to
try a different question against the same transcript.

Uses a local model via Ollama (llama3.1:8b-instruct-q4_K_M), not a paid API:
    brew install ollama
    ollama serve &
    ollama pull llama3.1:8b-instruct-q4_K_M

Benchmarked on this machine: sequential 4.0s/doc, 2 concurrent workers 1.37s/doc
(2.9x), 4 and 6 workers no further gain -- the model itself is the ceiling
past 2 concurrent requests, not Python or the network. Default workers is 4
for a small safety margin over the measured plateau.

Same append-only, skip-what's-done checkpoint shape as every other script
in this pipeline, keyed on the OCR text's own token:

    python3 scripts/generate_ai_summaries.py --status
    python3 scripts/generate_ai_summaries.py

To split the backlog across two machines (e.g. this one is GPU/RAM-bound
running Ollama for another job too), pass --shard I/N on each: every
document is assigned to exactly one shard by a stable hash of its token, so
running --shard 0/2 on one machine and --shard 1/2 on another covers the
whole backlog once each with no overlap. Each machine needs its own copy of
data/ocr_pilot.jsonl and writes its own data/ai_summaries.jsonl; merge the
two output files (they're disjoint by tok, so a plain concatenation is
enough) once both finish.

    python3 scripts/generate_ai_summaries.py --shard 0/2
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IN_JSONL = os.path.join(ROOT, "data", "ocr_pilot.jsonl")
OUT_JSONL = os.path.join(ROOT, "data", "ai_summaries.jsonl")

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.1:8b-instruct-q4_K_M"

# 2 workers already captures the measured 2.9x speedup; 4 and 6 did not
# improve on it further in benchmarking, so this is a safety margin, not an
# attempt to push past a ceiling the model itself already sets.
DEFAULT_WORKERS = 4

CHECKPOINT_EVERY = 20

# Matches extract_text.STORE_CHARS exactly -- ocr_pilot.py never captures more
# than this per document in the first place, so anything lower than 4000 was
# discarding OCR text already paid for and sitting on disk for free. Raised
# from 3000 on 2026-09-17 after a production regression across 4,620 records
# found real, measurable per-character cost, but also found 59.5% of
# documents already exceeded 3000 chars -- meaning over half the corpus was
# being summarized from a partial view for no reason, not for speed either
# (the discarded chars were never re-OCR'd or re-fetched, just thrown away).
MAX_INPUT_CHARS = 4000

# The model returns this literal token rather than guessing when the OCR
# text is too garbled to summarize confidently -- validated in a 10-document
# manual test before this script was written. Stored as unclear=true rather
# than discarded, so a later pass can tell "tried and gave up" apart from
# "never attempted".
UNCLEAR_TOKEN = "UNCLEAR"

PROMPT_TEMPLATE = """You are helping summarize Nebraska state government contract documents for a public transparency database. The text below was extracted by OCR from a scanned document and may contain scanning errors.

Write ONE concise, factual sentence (max 40 words) describing what this document is and what it's for. Use only information present in the text. If the OCR text is too garbled or unclear to summarize confidently, respond with exactly: UNCLEAR

Document text:
---
{text}
---

One-sentence summary:"""


def ollama_available():
    try:
        urllib.request.urlopen("http://localhost:11434/api/version", timeout=5)
        return True
    except (urllib.error.URLError, OSError):
        return False


def summarize(text):
    prompt = PROMPT_TEMPLATE.format(text=text[:MAX_INPUT_CHARS])
    payload = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 100},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["response"].strip()


def load_source_texts():
    """{tok: (doc, text)} for every document ocr_pilot.jsonl actually OCR'd.

    Last entry wins per token, same rule as every other checkpoint reader in
    this pipeline -- ocr_pilot.jsonl is append-only too.
    """
    texts = {}
    if not os.path.exists(IN_JSONL):
        raise SystemExit(f"missing {IN_JSONL} -- run scripts/ocr_pilot.py first")
    with open(IN_JSONL, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            v = r.get("engines", {}).get("vision", {})
            text = v.get("text")
            if text:
                texts[r["tok"]] = (r["doc"], text)
    return texts


def load_checkpoint():
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
                  "of a partial line. That document is simply retried.")
            os.truncate(OUT_JSONL, start)
            break
        store[rec["tok"]] = rec
    return store


def append_checkpoint(records):
    if not records:
        return
    os.makedirs(os.path.dirname(OUT_JSONL), exist_ok=True)
    with open(OUT_JSONL, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


def shard_of(tok, n):
    """Stable 0..n-1 bucket for tok -- independent of process/machine, unlike
    Python's salted built-in hash(), so --shard I/N partitions identically
    everywhere it's run."""
    return int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % n


def parse_shard(spec):
    i, n = spec.split("/")
    i, n = int(i), int(n)
    if not (0 <= i < n):
        raise argparse.ArgumentTypeError(f"shard index must satisfy 0 <= I < N, got {spec}")
    return i, n


def summarize_one(tok, doc, text):
    t0 = time.time()
    try:
        response = summarize(text)
    except Exception as e:
        return {"tok": tok, "doc": doc, "error": str(e)[:200]}
    seconds = round(time.time() - t0, 2)
    unclear = response.strip() == UNCLEAR_TOKEN
    return {
        "tok": tok, "doc": doc, "model": MODEL, "seconds": seconds,
        "unclear": unclear,
        "summary": None if unclear else response,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                         help=f"concurrent Ollama requests (default {DEFAULT_WORKERS}); "
                              f"benchmarked plateau is 2, this adds a small margin")
    parser.add_argument("--status", action="store_true",
                         help="print progress and exit -- no model calls")
    parser.add_argument("--shard", type=parse_shard, default=None, metavar="I/N",
                         help="only process the I-th of N stable hash-partitioned "
                              "shards (0 <= I < N) -- for splitting the backlog "
                              "across machines, see module docstring")
    args = parser.parse_args()

    sources = load_source_texts()
    checkpoint = load_checkpoint()
    if args.shard is not None:
        i, n = args.shard
        sources = {tok: v for tok, v in sources.items() if shard_of(tok, n) == i}
    todo = [tok for tok in sources if tok not in checkpoint]

    if args.status:
        shard_note = f" (shard {args.shard[0]}/{args.shard[1]})" if args.shard else ""
        print(f"TOTAL_OCR_TEXT={len(sources):,} documents{shard_note}")
        print(f"DONE={len(sources) - len(todo):,}  REMAINING={len(todo):,}")
        return

    if todo and not ollama_available():
        print("ERROR: Ollama is not responding on localhost:11434. Start it with "
              "`ollama serve` (and `ollama pull llama3.1:8b-instruct-q4_K_M` if the "
              "model isn't pulled yet). Nothing was written.", file=sys.stderr)
        sys.exit(1)

    print(f"{len(sources):,} documents have OCR text, {len(sources) - len(todo):,} "
          f"already summarized, {len(todo):,} remaining. Workers: {args.workers}.")

    start = time.time()
    processed = 0
    errors = 0
    unclear_count = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for chunk_start in range(0, len(todo), CHECKPOINT_EVERY):
            chunk = todo[chunk_start: chunk_start + CHECKPOINT_EVERY]
            futures = [pool.submit(summarize_one, tok, sources[tok][0], sources[tok][1])
                       for tok in chunk]
            new_records = []
            for future in as_completed(futures):
                try:
                    rec = future.result()
                except Exception as e:
                    rec = {"error": f"worker crashed: {e}"[:200]}
                if "error" in rec:
                    errors += 1
                elif rec.get("unclear"):
                    unclear_count += 1
                new_records.append(rec)

            append_checkpoint(new_records)
            processed += len(new_records)

            elapsed = time.time() - start
            done_so_far = min(chunk_start + CHECKPOINT_EVERY, len(todo))
            rate = processed / elapsed if elapsed else 0
            print(f"  {done_so_far:,}/{len(todo):,}  errors={errors} unclear={unclear_count}  "
                  f"({rate:.2f}/s, {elapsed:.0f}s elapsed)")

    print(f"\nFinished: {processed:,} documents processed this run "
          f"({errors:,} errors, {unclear_count:,} marked unclear).")
    print(f"Cumulative total on disk: {len(checkpoint) + processed:,} documents.")


if __name__ == "__main__":
    main()
