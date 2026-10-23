#!/bin/bash
# Nightly local catch-up: OCR whatever became newly "scanned" since last time.
#
# Cannot live in daily.yml -- that runs on ubuntu-latest, and the OCR engine
# this project settled on (Apple's Vision framework, via ocrmac) only runs on
# macOS. Even Tesseract, which is cross-platform, would break the same rule
# pages.yml already states for extract_text.py: a heavy per-document download
# "will never live in CI". So this runs locally instead, on this Mac, from a
# launchd LaunchAgent (com.justindiep.ne-contracts.ocr-catchup, not in the
# repo) at 04:30, after the GitHub nightly has published its data release.
#
# launchd does not wake a sleeping Mac. A StartCalendarInterval job missed
# while the Mac slept runs at the next wake instead, which can be mid-workday;
# only a scheduled `pmset` wake changes that. The LaunchAgent runs this whole
# script under `caffeinate -i -s`, so the Mac does not idle-sleep part way.
#
# Steps, each incremental by its own checkpoint, so a nightly re-run only costs
# whatever changed since yesterday, never the whole corpus:
#   1. fetch          -- today's ne-contracts-hub-data-<YYYY-MM-DD> release
#                        (America/Chicago date): the CSVs and scrape_meta.json
#                        the GitHub nightly just published, into data/. This
#                        Mac used to scrape its own copy, which drifted from
#                        what the site shows (6,343 records only in CI's copy
#                        on 22 Oct 2026) and kept the state's server busy for
#                        23 minutes. --local-scrape (or NE_OCR_LOCAL_SCRAPE=1)
#                        brings back scrape.py <dataset> --daily instead.
#   2. build          -- build_site.py regenerates manifest.json + the payload
#                        under d/, which extract_text.py's load_targets() reads
#                        to find documents at all.
#   3. prune          -- after a good build, delete every d/<build> except the
#                        one manifest.json names (141 MB a night otherwise).
#   4. extract_text   -- doc_text.jsonl: skips tokens already in it
#   5. extract_scope  -- scope.jsonl, rebuilt from doc_text.jsonl and swapped
#                        in whole. The state's own words, so the "OCR stays
#                        local" rule below does not apply; it reaches the site
#                        once published with scripts/publish_scope.sh.
#   6. ocr            -- ocr_pilot.py: ocr_pilot.jsonl, skips tokens in it
#   7. ollama         -- start the local Ollama server if it is not up
#   8. summaries      -- generate_ai_summaries.py: ai_summaries.jsonl
#
# OCR-recovered text (step 6) stays local until there's a decision about
# surfacing it as a verbatim description. The AI summaries (step 8) are a
# different, explicitly-labeled kind of claim -- see that script's docstring --
# and ship separately, by hand, with scripts/publish_ai_summaries.sh.
#
# A failing step does not stop the ones after it. Each step's exit code and
# duration go to the log and to logs/ocr_status.json (rewritten atomically
# after every step, so a run that hangs or dies shows where it got to). The
# script exits with the first non-zero step code, so launchd's last exit
# status means something:
#     0   every step succeeded
#     1   a step failed (the log says which)
#     2   OCR or summaries ran, but more than half of tonight's items failed
#     3   today's hub-data release is not published yet; the other steps ran
#         on the data already here
#     4   the release exists but could not be downloaded or is incomplete;
#         nothing in data/ was replaced
#    75   another run holds data/.ocr.lock; this one did nothing at all
#
#     scripts/run_ocr_catchup.sh                 # what launchd runs
#     scripts/run_ocr_catchup.sh --local-scrape  # scrape instead of download
#     scripts/run_ocr_catchup.sh --dry-run       # print the steps, run nothing
set -uo pipefail
cd "$(dirname "$0")/.."
# Appended, not prepended: launchd's PATH lacks Homebrew (gh, ollama), and
# anything already on PATH -- a test's stand-ins -- should win over it.
export PATH="$PATH:/opt/homebrew/bin"

PY="./venv/bin/python3"
REPO="diepjustin/ne-contracts"
STATUS_FILE="logs/ocr_status.json"
HUB_FILES="nu_contracts.csv nu_purchase_orders.csv state_agencies.csv scrape_meta.json"

LOCAL_SCRAPE="${NE_OCR_LOCAL_SCRAPE:-0}"
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --local-scrape) LOCAL_SCRAPE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,62p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $arg (try --help)" >&2; exit 64 ;;
    esac
done

CAFF=""
if command -v caffeinate >/dev/null 2>&1; then
    CAFF="caffeinate -i"
fi

if [ "$LOCAL_SCRAPE" = "1" ]; then
    SOURCE="local-scrape"
else
    SOURCE="hub-release"
fi

STARTED=$(date +%Y-%m-%dT%H:%M:%S%z)
HEAD=$(git rev-parse HEAD 2>/dev/null || echo unknown)
STEP_ARGS=()
FIRST_FAIL=0
LAST_CODE=0
NEW_OCR=0
NEW_SUMMARIES=0

line_count() {
    if [ -f "$1" ]; then wc -l < "$1" | tr -d ' '; else echo 0; fi
}

write_status() {
    # $1: the finish time, or empty while the run is still going.
    [ "$DRY_RUN" = "1" ] && return 0
    local extra=()
    if [ -n "$1" ]; then
        extra=(--finished "$1" --exit-code "$FIRST_FAIL")
    fi
    "$PY" scripts/ocr_io.py status --out "$STATUS_FILE" --started "$STARTED" \
        --head "$HEAD" --source "$SOURCE" \
        --count "new_ocr_records=$NEW_OCR" --count "new_summary_records=$NEW_SUMMARIES" \
        ${STEP_ARGS[@]+"${STEP_ARGS[@]}"} ${extra[@]+"${extra[@]}"} \
        || echo "warning: could not write $STATUS_FILE"
}

run_step() {
    local name="$1" start code secs
    shift
    echo "--- $name: $* ---"
    if [ "$DRY_RUN" = "1" ]; then
        LAST_CODE=0
        return 0
    fi
    start=$(date +%s)
    "$@"
    code=$?
    secs=$(( $(date +%s) - start ))
    echo "--- $name: exit $code after ${secs}s ---"
    STEP_ARGS+=(--step "$name=$code:$secs")
    if [ "$code" -ne 0 ] && [ "$FIRST_FAIL" -eq 0 ]; then
        FIRST_FAIL=$code
    fi
    LAST_CODE=$code
    write_status ""
    return 0
}

fetch_hub_release() {
    local day tag tmp err f
    day=$(TZ=America/Chicago date +%Y-%m-%d)
    tag="${NE_OCR_HUB_TAG:-ne-contracts-hub-data-$day}"
    tmp="data/.hub-download"
    rm -rf "$tmp" && mkdir -p "$tmp" || return 1

    if ! err=$(gh release download "$tag" --repo "$REPO" --dir "$tmp" --pattern '*.gz' 2>&1 >/dev/null); then
        rm -rf "$tmp"
        case "$err" in
            *"release not found"*|*"Not Found"*|*"HTTP 404"*)
                echo "today's release $tag is not published yet (the nightly publishes" \
                     "it around 03:17 Central). Carrying on with the data already in data/."
                return 3 ;;
        esac
        echo "could not download $tag; nothing in data/ was replaced: $err"
        return 4
    fi

    # All four or none: the CSVs and the stamps in scrape_meta.json describe
    # one night together, and a half-replaced set would claim otherwise.
    for f in $HUB_FILES; do
        if [ ! -s "$tmp/$f.gz" ] || ! gunzip -t "$tmp/$f.gz" 2>/dev/null; then
            echo "$tag has no usable $f.gz; nothing in data/ was replaced"
            rm -rf "$tmp"
            return 4
        fi
    done
    for f in $HUB_FILES; do
        if ! gunzip -c "$tmp/$f.gz" > "$tmp/$f"; then
            echo "could not unpack $f.gz; nothing in data/ was replaced"
            rm -rf "$tmp"
            return 4
        fi
    done
    for f in $HUB_FILES; do
        mv -f "$tmp/$f" "data/$f" || { rm -rf "$tmp"; return 1; }
    done
    rm -rf "$tmp"
    echo "installed $tag: $HUB_FILES"
    return 0
}

scrape_locally() {
    local dataset code first=0
    for dataset in contract purchase-order state; do
        "$PY" scripts/scrape.py "$dataset" --daily
        code=$?
        if [ "$code" -ne 0 ] && [ "$first" -eq 0 ]; then first=$code; fi
    done
    return "$first"
}

start_ollama() {
    if curl -s --max-time 5 http://localhost:11434/api/version > /dev/null 2>&1; then
        echo "ollama is already up"
        return 0
    fi
    # -np 2 (2 parallel generation slots) is a measured choice, not a default:
    # benchmarked at 3.0s/doc against 4.0s/doc serialized (-np 1), a real but
    # modest 25% gain -- this Mac only has 16GB RAM and the model alone holds
    # ~6GB of it, so pushing parallelism further risks swapping rather than
    # buying more throughput. --workers 2 on the script side matches it; more
    # script-side concurrency than server-side slots just queues.
    #
    # 9>&- keeps the lock's fd out of the server: it outlives this run, and
    # holding that fd open would leave every later run finding the lock taken.
    # The log is appended to, never truncated.
    echo "starting ollama serve (OLLAMA_NUM_PARALLEL=2)..."
    OLLAMA_NUM_PARALLEL=2 nohup ollama serve >> logs/ollama_serve.log 2>&1 9>&- &
    disown
    # A cold start can take longer than the five seconds this used to sleep;
    # ask it instead, for up to a minute.
    local i
    for i in $(seq 1 60); do
        if curl -s --max-time 2 http://localhost:11434/api/version > /dev/null 2>&1; then
            echo "ollama answered after ${i}s"
            return 0
        fi
        sleep 1
    done
    echo "ollama did not answer on localhost:11434 within 60s"
    return 1
}

mkdir -p logs data
echo "=== $(date) : starting OCR catch-up ($SOURCE, HEAD $HEAD) ==="

if [ "$DRY_RUN" != "1" ]; then
    # One run at a time. The shell holds the lock on fd 9 for the whole run --
    # flock belongs to the open file, so it outlives the helper that takes it --
    # and the Python steps re-take it on that same fd (NE_OCR_LOCK_FD). A busy
    # lock exits before touching anything, the status file included: that
    # belongs to the run in progress.
    exec 9>>data/.ocr.lock
    "$PY" scripts/ocr_io.py lock --fd 9
    code=$?
    if [ "$code" -eq 75 ]; then
        echo "another OCR run holds data/.ocr.lock; exiting without doing anything"
        exit 75
    elif [ "$code" -ne 0 ]; then
        echo "could not take data/.ocr.lock (exit $code); exiting without doing anything"
        exit "$code"
    fi
    export NE_OCR_LOCK_FD=9
fi

OCR_BEFORE=$(line_count data/ocr_pilot.jsonl)
SUMMARIES_BEFORE=$(line_count data/ai_summaries.jsonl)
write_status ""

if [ "$LOCAL_SCRAPE" = "1" ]; then
    run_step fetch scrape_locally
else
    run_step fetch fetch_hub_release
fi

run_step build "$PY" scripts/build_site.py
if [ "$LAST_CODE" -eq 0 ]; then
    run_step prune "$PY" scripts/ocr_io.py prune-builds
fi

run_step extract_text "$PY" scripts/extract_text.py
run_step extract_scope "$PY" scripts/extract_scope.py

# --sample comfortably above the current ~109k scanned corpus so a run
# always covers everything outstanding, not a fixed-size slice of it.
run_step ocr $CAFF "$PY" scripts/ocr_pilot.py --engines vision --sample 999999 --workers 6
NEW_OCR=$(( $(line_count data/ocr_pilot.jsonl) - OCR_BEFORE ))

run_step ollama start_ollama
run_step summaries $CAFF "$PY" scripts/generate_ai_summaries.py --workers 2
NEW_SUMMARIES=$(( $(line_count data/ai_summaries.jsonl) - SUMMARIES_BEFORE ))

write_status "$(date +%Y-%m-%dT%H:%M:%S%z)"
echo "=== $(date) : done, exit $FIRST_FAIL (new OCR records $NEW_OCR, new summary records $NEW_SUMMARIES) ==="
exit "$FIRST_FAIL"
