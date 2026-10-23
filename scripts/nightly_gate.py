"""Has tonight's scrape already finished? The idempotent gate at the top of daily.yml.

daily.yml has two cron entries, both off the hour, and both fire every night. The first one
to get through scrapes; the second is a backup. This decides which: it reads a
scrape_meta.json -- in the workflow, the one in the newest ne-contracts-hub-data-* release,
which every run that had a usable data/ publishes -- and exits 0 ("already done, skip")
only when all three datasets have a stamp at or after the start of tonight's night:

    python3 scripts/nightly_gate.py --meta /tmp/gate/scrape_meta.json

Any other outcome exits non-zero, and the workflow then scrapes: a dataset unfinished, the
file missing or unreadable, or this check failing in any way. Running twice is harmless
(--daily resumes and the release is replaced); skipping a night is the failure this
project keeps having, so every doubt resolves to "run".

A night starts at **noon America/Chicago**, and lasts until the next noon. The entries are
set for the evening, and the scheduler starts them hours late (4-5 h most nights in Oct
2026, 11.8 h at worst), so a night's scrape lands anywhere from about 9 pm to the next
morning -- always inside one noon-to-noon window, and never near its edges. Two things
that look simpler both lose real nights:

- "Which cron entry fired" (what this replaced, ci-07/sc-03): at today's delays the
  14 Mar 2027 spring-forward makes both entries skip, and the night is lost with two
  green runs.
- "Is there a release dated today (Chicago)": the release is dated when it is made, so a
  scrape that ran at 3 am on the 23rd makes a release dated the 23rd, and an on-time
  10:17 pm run on the 23rd would then find "today's" release and skip the whole night.

Standard library only (zoneinfo needs the system tz database, which ubuntu-latest and
macOS have), because it runs before setup-python.
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_scrape_stamps  # noqa: E402

CHICAGO = "America/Chicago"


def night_start(now):
    """The most recent noon in Chicago at or before `now` (an aware datetime), as an aware datetime."""
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(CHICAGO))
    noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
    if local < noon:
        # Aware arithmetic is wall-clock arithmetic, and zoneinfo works out the offset for the
        # new date; no DST change happens anywhere near noon.
        noon -= datetime.timedelta(days=1)
    return noon


def main(argv=None):
    parser = argparse.ArgumentParser(description="Exit 0 only if every dataset already finished tonight.")
    parser.add_argument("--meta", required=True, help="a scrape_meta.json, e.g. from the newest hub-data release")
    parser.add_argument("--now", default=None, help="ISO-8601 with an offset; for tests (default: the clock)")
    args = parser.parse_args(argv)

    try:
        now = (check_scrape_stamps.parse_time(args.now) if args.now
               else datetime.datetime.now(datetime.timezone.utc))
        since = night_start(now)
        stamps, unreadable = check_scrape_stamps.read_stamps(args.meta)
        found = check_scrape_stamps.problems(stamps, since, unreadable)
    except Exception as e:  # noqa: BLE001 -- any doubt means "run"
        print(f"gate check failed ({type(e).__name__}: {e}); running to be safe")
        return 2

    if found:
        # Plain lines, not ::error -- a night that still needs its scrape is the normal case.
        print(f"tonight's night began {since.isoformat()}; not finished yet, so this run scrapes:")
        for dataset, why in found:
            print(f"  {dataset}: {why}")
        return 1
    print(f"tonight's night began {since.isoformat()}, and every dataset has finished since:")
    for dataset in check_scrape_stamps.DATASETS:
        print(f"  {dataset}: {stamps[dataset]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
