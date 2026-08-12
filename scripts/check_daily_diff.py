"""Guard rail for --daily publishing.

Reads the week's accumulated data/daily_diff_report.json (one entry per
dataset per day, written by scrape.py's --daily mode) and refuses to bless
the week if any single day's Active -> Expired drop, for any one entity,
looks more like a scraping failure than real-world contract expiration.

Exit 0: nothing implausible found, safe to build and publish.
Exit 1: something implausible found -- the workflow step fails, which is
what stops the commit/push step from running and triggers GitHub's built-in
scheduled-workflow-failure email. The report is cleared either way, so next
week starts counting from zero; the failure is on record in this run's logs.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(ROOT, "data", "daily_diff_report.json")

# Losing half an entity's active documents in a single day is far outside
# plausible staggered-expiration churn -- contracts and purchase orders run
# months to years with end dates spread across the calendar (see HANDOFF.md's
# data caveats), so natural single-day attrition should be a few percent at
# most. A drop this size is much more likely a scrape failure (empty page
# misread as zero results, a renamed/retired entity -- see
# check_entity_drift.py) than reality.
MAX_SINGLE_DAY_FLIP_FRACTION = 0.5

# Below this many previously-active records, percentages are noise (1 of 2
# expiring is 50% and means nothing). Skip entities under this baseline.
MIN_BASELINE = 5


def main():
    if not os.path.exists(REPORT):
        print("No daily diff report found -- nothing accumulated this week, nothing to check.")
        return 0

    with open(REPORT, encoding="utf-8") as f:
        entries = json.load(f)

    violations = []
    for entry in entries:
        for name, counts in entry["entities"].items():
            base, flipped = counts["previously_active"], counts["flipped_to_expired"]
            if base >= MIN_BASELINE and flipped / base > MAX_SINGLE_DAY_FLIP_FRACTION:
                violations.append((entry["dataset"], entry["timestamp"], name, flipped, base))

    if violations:
        print(f"GUARD RAIL FAILED: {len(violations)} entity/day combination(s) look implausible:")
        for dataset, ts, name, flipped, base in violations:
            print(f"  [{dataset} @ {ts}] {name}: {flipped}/{base} ({flipped/base:.0%}) flipped Active -> Expired")
        print("\nNot publishing this week. Investigate before the next scheduled run -- check "
              "scripts/check_entity_drift.py first (a renamed/retired entity looks exactly like "
              "this: previously_active > 0, seen = 0, 100% flipped).")
        clear_report()
        return 1

    print(f"Guard rail passed: {len(entries)} dataset-run(s) checked this week, nothing implausible.")
    clear_report()
    return 0


def clear_report():
    if os.path.exists(REPORT):
        os.remove(REPORT)


if __name__ == "__main__":
    sys.exit(main())
