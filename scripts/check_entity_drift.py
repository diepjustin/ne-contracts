"""Compare the committed entity lists against what the state's site serves today.

The entity lists are committed as static data (scripts/state_entities.json and
scrape.py's HIGHER_ED_ENTITIES) rather than fetched at scrape time, so a scrape
never depends on an extra live request. The tradeoff is that the state can add,
rename, or retire an agency without us noticing.

It only reports -- it never edits the committed lists. Two ways to run it:

    python3 scripts/check_entity_drift.py            # nightly, in daily.yml: warns, exits 0
    python3 scripts/check_entity_drift.py --strict   # by hand before a full re-scrape: exits 1

By default drift, and a failure to ask (after one retry), print
`::warning title=Entity list drift::...` annotations and exit 0. It used to exit 1 on
either, and it runs in daily.yml before the guard rail and the deploy, so one new agency on
the state's list, or one dropped connection, stopped the site updating until someone
edited state_entities.json (audit sc-02). A new agency is not a reason to stop publishing:
--daily never reads it, so nothing about tonight's data is wrong, only incomplete, and the
warning says so every night until the lists are updated. --strict keeps the old "loud
safety net you invoke deliberately" behaviour, the role verify() plays in build_site.py.
"""

import argparse
import json
import os
import sys
import re
import time
import html as htmlmod

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
STATE_ENTITIES_JSON = os.path.join(SCRIPTS, "state_entities.json")

ENTITIES_URL = "https://statecontracts.nebraska.gov/Search/GetEntitiesByType"

# Not real agencies -- "ALL" is a search-all pseudo-entity, and the blank option
# is the dropdown's placeholder.
SKIP_VALUES = {"", "ALL-999-000"}

TITLE = "Entity list drift"
RETRY_DELAY = 30    # seconds before the one retry; a blip, not an outage, is what a retry is for

# A live list this much shorter than the committed one is a broken answer, not 30 agencies
# retired overnight. Reporting it as drift would print a REMOVED line for every agency.
MIN_LIVE_SHARE = 0.5


class FetchError(Exception):
    pass


def fetch_entities(query_type):
    """{display_name: entity_value} as the site serves it right now."""
    sys.path.insert(0, SCRIPTS)
    from scrape import USER_AGENT  # noqa: E402 -- one definition, see scrape.py
    resp = requests.get(
        ENTITIES_URL,
        params={"queryType": query_type},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    opts = re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)</option>', resp.text)
    return {
        htmlmod.unescape(text).strip(): htmlmod.unescape(value)
        for value, text in opts
        if value not in SKIP_VALUES
    }


def fetch_checked(query_type, committed):
    """fetch_entities, retried once, with an implausible answer counted as a failure."""
    last = None
    for attempt in range(2):
        if attempt:
            time.sleep(RETRY_DELAY)
        try:
            live = fetch_entities(query_type)
        except requests.exceptions.RequestException as e:
            last = f"{type(e).__name__}: {e}"
            continue
        if len(live) < len(committed) * MIN_LIVE_SHARE:
            last = (f"the site listed {len(live)} entit(y/ies) against {len(committed)} committed, "
                    "which is a broken answer rather than drift")
            continue
        return live
    raise FetchError(last)


def warn(message):
    print(f"::warning title={TITLE}::{message}")


def diff(label, committed, live):
    added = sorted(set(live) - set(committed))
    removed = sorted(set(committed) - set(live))
    changed = sorted(n for n in set(committed) & set(live) if committed[n] != live[n])

    print(f"\n{label}: {len(committed)} committed, {len(live)} live")
    if not (added or removed or changed):
        print("  no drift")
        return False

    for name in added:
        print(f"  ADDED    {name!r} -> {live[name]!r}")
        warn(f"{label}: the state added {name!r} ({live[name]!r}); --daily does not read it "
             "until it is in the committed list")
    for name in removed:
        print(f"  REMOVED  {name!r} (was {committed[name]!r})")
        warn(f"{label}: the state no longer lists {name!r} (was {committed[name]!r})")
    for name in changed:
        print(f"  CHANGED  {name!r}: {committed[name]!r} -> {live[name]!r}")
        warn(f"{label}: {name!r} changed value {committed[name]!r} -> {live[name]!r}")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare the committed entity lists with the state's.")
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 on drift or on a failure to ask -- for use by hand before a "
                             "full re-scrape. Without it both are warnings and the exit is 0.")
    args = parser.parse_args(argv)

    with open(STATE_ENTITIES_JSON, encoding="utf-8") as f:
        committed_state = json.load(f)

    sys.path.insert(0, SCRIPTS)
    from scrape import HIGHER_ED_ENTITIES  # noqa: E402 -- committed list lives with the scraper

    drifted = failed = False
    for label, query_type, committed in (("State", "State", committed_state),
                                         ("Higher Education", "Higher Education", HIGHER_ED_ENTITIES)):
        try:
            live = fetch_checked(query_type, committed)
        except FetchError as e:
            failed = True
            print(f"\n{label}: could not be checked")
            warn(f"{label}: could not ask the state for its entity list, twice ({e}). "
                 "Not checked tonight.")
            continue
        drifted |= diff(label, committed, live)

    if drifted:
        print(
            f"\nDrift found. Update {STATE_ENTITIES_JSON} and/or HIGHER_ED_ENTITIES in "
            "scripts/scrape.py before re-scraping -- build_site.py hard-fails on any "
            "entity name in the CSVs that isn't in its ENTITIES list."
        )
    elif not failed:
        print("\nNo drift in either list.")
    return 1 if args.strict and (drifted or failed) else 0


if __name__ == "__main__":
    sys.exit(main())
