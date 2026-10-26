"""Did tonight's scrape actually finish? The check that turns an unfinished night red.

scrape.py stamps a dataset in data/scrape_meta.json only when every entity in it has been
read (`record_scrape_time`). A night that does not finish -- an entity refused because the
state stopped listing most of its records, a deadline, a crash after the other datasets were
done -- leaves that stamp where it was, exits 0, and the job ends green. Nothing then looks at
the stamp until a person notices the page's "Last updated" date has not moved. The 24 Sep 2026
freeze went 19 nights that way.

This reads the three stamps and compares each with the moment *tonight began*:

    python3 scripts/check_scrape_stamps.py --since 2026-10-13T12:00:00-05:00

It exits 1 and prints one `::error title=Daily scrape did not finish::<dataset>: <why>`
annotation per dataset whose stamp is missing, unreadable, has no UTC offset, or is earlier
than --since. Exit 0 means all three datasets finished tonight.

Compared with the start of the night, not with "now" or a fixed age, on purpose. A fixed
threshold has to sit between a normal night (stamps minutes old) and a missed one (about 25
hours), and the cron's own lateness (a fire can slip hours) eats that margin; "earlier than
tonight began" has no margin to eat. A missed night is caught the first time it happens.
Tonight is nightly_gate.py's noon-to-noon Central night. Until Oct 2026 this compared with
the moment the run started, which a backup run cannot meet for a dataset the first run
already finished: each scrape step now skips such a dataset rather than read it again.

It is the LAST step of daily.yml, after the deploy dispatch, the cache save and the release,
so it can colour the run red without blocking any of them -- the red run is what sends
GitHub's failed-scheduled-run email. The gate step records when tonight began (`night`,
from `nightly_gate.py --night-start`), and this step takes it through `env`, never by
interpolating it into the command line,
with `if: always() && steps.gate.outputs.proceed == 'true'`. The condition names no step id, so
there is none to forget to add; `always()` means it also reports on a night an earlier step
already failed, which only repeats what is already red. If the gate step stopped recording
`night`, --since would arrive empty and this would fail every night, loudly.

Standard library only, so it runs before or without `pip install`, and it never raises: bad
input is reported as a problem, and a failure of the check itself is reported and exits 1.
"""
import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META = os.path.join(ROOT, "data", "scrape_meta.json")

# The datasets scrape.py stamps, as it names them. A literal rather than an import so this
# stays standard-library only; tests/test_check_scrape_stamps.py pins it to scrape.DATASETS
# and build_site.DATA, which is where the page reads the same three stamps.
DATASETS = ("contract", "purchase-order", "state")

TITLE = "Daily scrape did not finish"


def parse_time(text):
    """An ISO-8601 timestamp with a UTC offset, as an aware datetime. Raises ValueError otherwise."""
    if not isinstance(text, str):
        raise ValueError(f"expected text, found {type(text).__name__}")
    text = text.strip()
    # fromisoformat did not take a trailing Z before Python 3.11.
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    moment = datetime.datetime.fromisoformat(text)
    if moment.utcoffset() is None:
        raise ValueError("it has no UTC offset, so it cannot be compared with another moment")
    return moment


def read_stamps(path):
    """({dataset: stamp}, why-it-could-not-be-read or None). Never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            stamps = json.load(f)
    except (OSError, ValueError) as e:
        return {}, f"{os.path.basename(path)} could not be read ({type(e).__name__}: {e})"
    if not isinstance(stamps, dict):
        return {}, f"{os.path.basename(path)} holds {type(stamps).__name__}, not an object"
    return stamps, None


def problems(stamps, since, unreadable=None):
    """[(dataset, why), ...] for every dataset that did not finish since `since`."""
    found = []
    for dataset in DATASETS:
        if unreadable:
            found.append((dataset, f"no stamp: {unreadable}"))
            continue
        if dataset not in stamps:
            found.append((dataset, "no stamp -- this dataset has never finished a scrape here"))
            continue
        raw = stamps[dataset]
        try:
            moment = parse_time(raw)
        except ValueError as e:
            found.append((dataset, f"its stamp {str(raw)[:60]!r} is not usable: {e}"))
            continue
        if moment < since:
            found.append((dataset, f"last finished {raw}, before tonight began at "
                                   f"{since.isoformat()}; tonight's scrape did not complete it"))
    return found


def escape(text):
    """GitHub's escaping for a workflow command's message, so data cannot start a new command."""
    return str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fail unless every dataset's scrape finished since --since.")
    parser.add_argument("--since", required=True,
                        help="when tonight began: ISO-8601 with a UTC offset, e.g. 2026-10-13T12:00:00-05:00")
    parser.add_argument("--meta", default=META, help="the stamps file (default: data/scrape_meta.json)")
    args = parser.parse_args(argv)

    try:
        try:
            since = parse_time(args.since)
        except ValueError as e:
            print(f"::error title={TITLE}::{escape('--since ' + repr(args.since) + ' is not usable: ' + str(e))}")
            return 1

        stamps, unreadable = read_stamps(args.meta)
        found = problems(stamps, since, unreadable)
        for dataset, why in found:
            print(f"::error title={TITLE}::{escape(dataset + ': ' + why)}")
        if found:
            return 1

        for dataset in DATASETS:
            print(f"{dataset}: finished {stamps[dataset]}")
        print(f"All {len(DATASETS)} datasets finished since tonight began at {since.isoformat()}.")
        return 0
    except Exception as e:  # noqa: BLE001 -- this check must say what went wrong, not die with a traceback
        print(f"::error title={TITLE}::{escape('check_scrape_stamps.py itself failed: ' + type(e).__name__ + ': ' + str(e))}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
