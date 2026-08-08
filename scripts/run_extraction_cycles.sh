#!/bin/bash
# Runs scripts/extract_text.py in bounded work chunks with a rest period
# between them, so a multi-hour full-corpus run doesn't hold sustained load
# against the state's server for a day straight. Safe to kill and restart
# at any point -- extract_text.py checkpoints to data/doc_text.json after
# every 10 documents and resumes from there.
#
# Usage: scripts/run_extraction_cycles.sh [work_hours] [pause_minutes]

set -euo pipefail
cd "$(dirname "$0")/.."

WORK_HOURS="${1:-3}"
PAUSE_MINUTES="${2:-30}"

source venv/bin/activate

cycle=1
while true; do
    echo "=== cycle $cycle: working for ${WORK_HOURS}h ==="
    python3 scripts/extract_text.py --hours "$WORK_HOURS"

    remaining=$(python3 scripts/extract_text.py --status | grep REMAINING= | cut -d= -f2)
    echo "=== cycle $cycle done, $remaining documents remaining ==="

    if [ "$remaining" -eq 0 ]; then
        echo "=== ALL DOCUMENTS PROCESSED ==="
        break
    fi

    echo "=== pausing ${PAUSE_MINUTES}m before next cycle ==="
    sleep "$(awk "BEGIN { print $PAUSE_MINUTES * 60 }")"
    cycle=$(( cycle + 1 ))
done
