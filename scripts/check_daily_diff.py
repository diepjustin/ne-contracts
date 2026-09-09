"""Guard rail for --daily publishing.

Reads data/daily_diff_report.json (one entry per dataset per run, written by
scrape.py's --daily mode) and refuses to bless the data if any single day's
Active -> Expired drop, for any one entity, looks more like a scraping
failure than real-world contract expiration.

Run once a night, after the scrape and before the deploy is dispatched, so
in the ordinary case it is reading the three entries the evening just wrote.
It read a whole week's worth until Sep 2026, when publishing went nightly;
the check itself is per entity per day either way, so what changed is how
soon a bad night is caught, not what counts as bad.

Exit 0: nothing implausible found, safe to build and publish.
Exit 1: something implausible found -- the workflow step fails, which is
what stops the deploy from being dispatched and triggers GitHub's built-in
scheduled-workflow-failure email. The report is cleared either way, so the
next run starts counting from zero; the failure is on record in this run's
logs. Note what that means: the rejected night is not re-examined tomorrow.
Holding data back from the *site* was never the real protection -- the
refusals inside scrape.py are, because they run where the write happens.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(ROOT, "data", "daily_diff_report.json")

# Losing half an entity's active documents in a single day is far outside
# plausible staggered-expiration churn -- contracts and purchase orders run
# months to years with end dates spread across the calendar (see README.md's
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
        print("No daily diff report found -- nothing accumulated since the last publish, "
              "nothing to check.")
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
        print("\nNot publishing tonight. Investigate before the next scheduled run -- check "
              "scripts/check_entity_drift.py first (a renamed/retired entity looks exactly like "
              "this: previously_active > 0, seen = 0, 100% flipped).")
        clear_report()
        return 1

    print(f"Guard rail passed: {len(entries)} dataset-run(s) checked since the last publish, "
          "nothing implausible.")
    clear_report()
    return 0


def clear_report():
    if os.path.exists(REPORT):
        os.remove(REPORT)


if __name__ == "__main__":
    sys.exit(main())
