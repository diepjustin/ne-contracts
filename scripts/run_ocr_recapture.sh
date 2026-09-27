#!/bin/bash
# One-time (not nightly) full-page OCR recapture, run under launchd so it
# survives crashes and reboots unattended -- see
# com.justindiep.ne-contracts.ocr-recapture.plist.
#
# Recaptures the documents scripts/ocr_pilot.py originally truncated at 6
# pages / 4,000 characters, now that its defaults are 200 pages / 100,000
# characters (see that script's DEFAULT_MAX_PAGES/DEFAULT_STORE_CHARS
# docstring for why). Checkpointed and idempotent: once every document is
# done, running this again just reports 0 remaining and exits 0 immediately
# -- safe for launchd's KeepAlive to call again if the Mac restarts before
# then, and harmless busywork after.
#
# Deliberately its own launchd job rather than folded into
# run_ocr_catchup.sh: that one is nightly-recurring and covers newly-scraped
# documents (which already get the higher limits via ocr_pilot.py's updated
# defaults, no separate step needed); this one is a finite backlog that
# only needs to run until it drains, then never again.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:$PATH"

echo "=== $(date) : OCR recapture (launchd) ==="
caffeinate -i ./venv/bin/python3 scripts/ocr_pilot.py --engines vision --sample 999999 --workers 6
status=$?
echo "=== $(date) : recapture run exited $status ==="
# Propagate the real exit code -- launchd's KeepAlive/SuccessfulExit decides
# whether to restart based on THIS script's exit status, not anything printed
# above it. A script that always exited 0 would make a real crash look like
# a clean finish and launchd would stop retrying it.
exit "$status"
