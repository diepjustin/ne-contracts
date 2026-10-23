"""--daily across several nights: subsets, restores, held-back rows and strays that persist.

test_status_flips.py pins one night's decisions. These pin what those decisions add up to
over a run of nights, the cases the Oct 2026 audit found unpinned (diff-01, diff-02, diff-06,
sc-04, sc-06): a `--entity` run stamping a dataset it did not read, a restored record keeping
the amount and end date it had when it left the list, a leaky night appending records the
state lists as Expired, and strays that never go away freezing an entity's flip-backs.

Nothing here touches the network: the search is scripted by test_status_flips.night().
"""

import json
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.join(os.path.dirname(TESTS), "scripts"))

import scrape  # noqa: E402
from test_status_flips import (CAMPUS, OTHER, listed, night, report, row,  # noqa: E402
                               stamped, status_of)


# --- sc-06: a subset run never stamps the dataset -------------------------------------------

def test_an_entity_subset_does_not_stamp_the_whole_dataset(monkeypatch, tmp_path, capsys):
    rows = [row(n) for n in range(3)] + [row(99, OTHER)]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(3)]}, only=(CAMPUS,))

    assert not stamped(tmp_path), "OTHER was never read, so the dataset is not fresh"
    [entry] = report(tmp_path)
    assert entry["complete"] is False and set(entry["entities"]) == {CAMPUS}
    printed = capsys.readouterr().out
    assert "limited by --entity" in printed and OTHER in printed


def test_a_subset_that_finishes_the_rest_of_the_day_does_stamp(monkeypatch, tmp_path):
    """The subset is not the problem; completeness against the full list is the test."""
    rows = [row(n) for n in range(3)] + [row(99, OTHER)]
    search = {CAMPUS: [listed(n) for n in range(3)], OTHER: [listed(99, OTHER)]}
    night(monkeypatch, tmp_path, rows, search, only=(CAMPUS,))
    night(monkeypatch, tmp_path, None, search, only=(OTHER,))

    assert stamped(tmp_path)
    [entry] = report(tmp_path)
    assert entry["complete"] is True and set(entry["entities"]) == {CAMPUS, OTHER}
