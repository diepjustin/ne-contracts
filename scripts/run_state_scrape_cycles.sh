#!/bin/bash
# Runs the state-agency scrape in bounded work chunks with a rest period
# between them. The full run covers 83 agencies over many hours; chunking
# keeps us from holding sustained load against the state's server for a
# whole day straight, and means a failure costs one cycle rather than the
# whole run.
#
# Safe to kill and restart at any point -- scrape.py checkpoints completed
# (entity, status) combos to data/state_scrape_progress.json and discards
# partial rows from any combo interrupted mid-scrape.
#
# Usage: scripts/run_state_scrape_cycles.sh [work_hours] [pause_minutes]

set -euo pipefail
cd "$(dirname "$0")/.."

WORK_HOURS="${1:-3}"
PAUSE_MINUTES="${2:-30}"

source venv/bin/activate

cycle=1
while true; do
    echo "=== cycle $cycle: working for ${WORK_HOURS}h ==="
    python3 scripts/scrape.py state --hours "$WORK_HOURS"

    remaining=$(python3 scripts/scrape.py state --status | grep REMAINING= | cut -d= -f2)
    echo "=== cycle $cycle done, $remaining combos remaining ==="

    if [ "$remaining" -eq 0 ]; then
        echo "=== ALL COMBOS SCRAPED ==="
        break
    fi

    echo "=== pausing ${PAUSE_MINUTES}m before next cycle ==="
    sleep "$(awk "BEGIN { print $PAUSE_MINUTES * 60 }")"
    cycle=$(( cycle + 1 ))
done
