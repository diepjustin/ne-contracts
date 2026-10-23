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


# --- diff-03: the flip ledger is hashed and pruned ------------------------------------------

def _url(n):
    return f"https://example.test/detail?D={n}"


def test_a_ledger_of_raw_urls_loads_under_hashed_keys(tmp_path):
    path = tmp_path / "flips.json"
    path.write_text(json.dumps({_url(1): "2026-10-20", scrape.ledger_key(_url(2)): "2026-10-21"}),
                    encoding="utf-8")
    assert scrape.load_flip_ledger(str(path)) == {scrape.ledger_key(_url(1)): "2026-10-20",
                                                  scrape.ledger_key(_url(2)): "2026-10-21"}


def test_ledger_keys_are_the_expired_only_keys():
    assert scrape.ledger_key(_url(1)) == scrape.document_key(_url(1))
    assert scrape.ledger_key(scrape.ledger_key(_url(1))) == scrape.ledger_key(_url(1))


def test_pruning_drops_old_entries_and_ones_no_longer_expired_only(tmp_path):
    import datetime

    today = datetime.date(2026, 10, 23)
    path = tmp_path / "flips.json"
    path.write_text(json.dumps({
        _url(1): "2026-10-22",                     # owed, recent: kept
        _url(2): "2025-10-22",                     # 366 days old: dropped
        _url(3): "2025-10-23",                     # exactly the limit: kept
        _url(4): "2026-10-22",                     # its row is Active again: dropped
        _url(5): "not a date",                     # unreadable date: kept, doubt keeps it
    }), encoding="utf-8")
    expired_only = {scrape.document_key(_url(n)) for n in (1, 2, 3, 5)}
    pruned = scrape.load_flip_ledger(str(path), expired_only, today=today)
    assert set(pruned) == {scrape.ledger_key(_url(n)) for n in (1, 3, 5)}


def test_a_night_saves_the_pruned_ledger(monkeypatch, tmp_path):
    """Record 19 was flipped and then re-read Active by hand: the next night's ledger drops it."""
    from test_status_flips import keys, ledger

    rows = [row(n) for n in range(20)]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(19)]}, entities=(CAMPUS,))
    assert set(ledger(tmp_path)) == keys(19)

    csv_path = tmp_path / "data" / "nu_contracts.csv"
    text = csv_path.read_text(encoding="utf-8").replace(",Expired,", ",Active,")
    csv_path.write_text(text, encoding="utf-8")            # row 19 Active again, outside --daily
    night(monkeypatch, tmp_path, None, {CAMPUS: [listed(n) for n in range(18)] + [listed(19)]},
          entities=(CAMPUS,))
    assert set(ledger(tmp_path)) == keys(18), "19's stale entry pruned, tonight's flip of 18 added"
