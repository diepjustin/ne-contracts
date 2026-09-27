#!/bin/bash
# Nightly local catch-up: OCR whatever became newly "scanned" since last time.
#
# Cannot live in daily.yml -- that runs on ubuntu-latest, and the OCR engine
# this project settled on (Apple's Vision framework, via ocrmac) only runs on
# macOS. Even Tesseract, which is cross-platform, would break the same rule
# pages.yml already states for extract_text.py: a heavy per-document download
# "will never live in CI". So this runs locally instead, on this Mac, via
# launchd (see com.justindiep.ne-contracts.ocr-catchup.plist) -- not cron,
# which cannot wake a sleeping Mac or catch up a missed run the way a
# StartCalendarInterval LaunchAgent does.
#
# Five steps, each already incremental by its own checkpoint, so a nightly
# re-run only costs whatever changed since yesterday, never the whole corpus:
#   1. scrape.py --daily        -- documents.jsonl/CSVs: skips known records
#   2. build_site.py            -- regenerates manifest.json + the payload
#                                   under d/, which extract_text.py's
#                                   load_targets() reads to find documents at
#                                   all. pages.yml gets this for free by
#                                   running the real site build in CI; this
#                                   local checkout has no such build step
#                                   otherwise, so extract_text.py would see
#                                   zero targets and do nothing, forever.
#   3. extract_text.py          -- doc_text.jsonl: skips tokens already in it
#   4. ocr_pilot.py             -- ocr_pilot.jsonl: skips tokens already in it
#   5. generate_ai_summaries.py -- ai_summaries.jsonl: skips tokens already in it
#
# Deliberately does not touch data/scope.jsonl or the extraction-data-*
# release. OCR-recovered text (step 4) stays local until there's a decision
# about surfacing it as a verbatim description; the AI summaries (step 5) are
# a different, explicitly-labeled kind of claim -- see that script's
# docstring -- and ship separately, not folded into scope.jsonl's "the
# state's own words" guarantee.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:$PATH"

echo "=== $(date) : starting OCR catch-up ==="

echo "--- 1/4: refresh document metadata ---"
./venv/bin/python3 scripts/scrape.py contract --daily
./venv/bin/python3 scripts/scrape.py purchase-order --daily
./venv/bin/python3 scripts/scrape.py state --daily

echo "--- 2/4: rebuild manifest.json so extract_text.py has targets ---"
./venv/bin/python3 scripts/build_site.py

echo "--- 3/4: classify any new documents' text layer ---"
./venv/bin/python3 scripts/extract_text.py

echo "--- 4/5: OCR anything still marked scanned ---"
# --sample comfortably above the current ~109k scanned corpus so a run
# always covers everything outstanding, not a fixed-size slice of it.
caffeinate -i ./venv/bin/python3 scripts/ocr_pilot.py --engines vision --sample 999999 --workers 6

echo "--- 5/5: generate AI summaries for anything newly OCR'd ---"
# -np 2 (2 parallel generation slots) is a measured choice, not a default:
# benchmarked at 3.0s/doc against 4.0s/doc serialized (-np 1), a real but
# modest 25% gain -- this Mac only has 16GB RAM and the model alone holds
# ~6GB of it, so pushing parallelism further risks swapping rather than
# buying more throughput. --workers 2 on the script side matches it; more
# script-side concurrency than server-side slots just queues, as measured
# earlier the same way against ocr_pilot.py's Vision engine.
if ! curl -s http://localhost:11434/api/version > /dev/null 2>&1; then
    echo "starting ollama serve (OLLAMA_NUM_PARALLEL=2)..."
    OLLAMA_NUM_PARALLEL=2 nohup ollama serve > logs/ollama_serve.log 2>&1 &
    disown
    sleep 5
fi
caffeinate -i ./venv/bin/python3 scripts/generate_ai_summaries.py --workers 2

echo "=== $(date) : done ==="
