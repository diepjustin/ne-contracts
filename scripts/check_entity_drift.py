"""Compare the committed entity lists against what the state's site serves today.

The entity lists are committed as static data (scripts/state_entities.json and
scrape.py's HIGHER_ED_ENTITIES) rather than fetched at scrape time, so a scrape
never depends on an extra live request. The tradeoff is that the state can add,
rename, or retire an agency without us noticing.

Run this by hand before a full re-scrape. It only reports -- it never edits the
committed lists, same "loud safety net you invoke deliberately" role that
verify() plays in build_site.py.
"""

import json
import os
import sys
import re
import html as htmlmod

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
STATE_ENTITIES_JSON = os.path.join(SCRIPTS, "state_entities.json")

ENTITIES_URL = "https://statecontracts.nebraska.gov/Search/GetEntitiesByType"

# Not real agencies -- "ALL" is a search-all pseudo-entity, and the blank option
# is the dropdown's placeholder.
SKIP_VALUES = {"", "ALL-999-000"}


def fetch_entities(query_type):
    """{display_name: entity_value} as the site serves it right now."""
    resp = requests.get(
        ENTITIES_URL,
        params={"queryType": query_type},
        headers={"User-Agent": "Mozilla/5.0 (compatible; research scraper)"},
        timeout=30,
    )
    resp.raise_for_status()
    opts = re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)</option>', resp.text)
    return {
        htmlmod.unescape(text).strip(): htmlmod.unescape(value)
        for value, text in opts
        if value not in SKIP_VALUES
    }


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
    for name in removed:
        print(f"  REMOVED  {name!r} (was {committed[name]!r})")
    for name in changed:
        print(f"  CHANGED  {name!r}: {committed[name]!r} -> {live[name]!r}")
    return True


def main():
    with open(STATE_ENTITIES_JSON, encoding="utf-8") as f:
        committed_state = json.load(f)

    sys.path.insert(0, SCRIPTS)
    from scrape import HIGHER_ED_ENTITIES  # noqa: E402 -- committed list lives with the scraper

    drifted = False
    drifted |= diff("State", committed_state, fetch_entities("State"))
    drifted |= diff("Higher Education", HIGHER_ED_ENTITIES, fetch_entities("Higher Education"))

    if drifted:
        print(
            f"\nDrift found. Update {STATE_ENTITIES_JSON} and/or HIGHER_ED_ENTITIES in "
            "scripts/scrape.py before re-scraping -- build_site.py hard-fails on any "
            "entity name in the CSVs that isn't in its ENTITIES list."
        )
        return 1

    print("\nNo drift in either list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
