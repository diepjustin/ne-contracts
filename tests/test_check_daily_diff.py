"""The guard rail on tonight's diff: what passes, what fails, and what is cleared.

check_daily_diff.py is the last thing standing between a scrape the state got
wrong and the published site, and until now only its unfinished-night wording
was tested -- and that only as a side effect of the scraper's own tests. Its
boundaries are single characters (half, five), and it clears the report on both
exits so one bad night cannot block every later publish; none of that was
pinned. Pure file-in, exit-code-out tests: no network, no data/.
"""

import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import check_daily_diff  # noqa: E402


def counts(previous, flipped):
    return {"previously_active": previous, "still_active": previous - flipped,
            "newly_active": 0, "flipped_to_expired": flipped}


def entry(entities, dataset="contract", complete=True):
    out = {"dataset": dataset, "timestamp": "2026-10-13T23:00:00-05:00", "entities": entities}
    if complete is not True:
        out["complete"] = complete
    return out


@pytest.fixture
def report(monkeypatch, tmp_path):
    """Write a report under tmp_path and point the guard rail at it."""
    path = tmp_path / "daily_diff_report.json"
    monkeypatch.setattr(check_daily_diff, "REPORT", str(path))

    def write(entries):
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    write.path = path
    return write


# --- the half boundary ------------------------------------------------------

def test_exactly_half_flipped_passes(report, capsys):
    report([entry({"A": counts(10, 5)})])
    assert check_daily_diff.main() == 0
    assert "Guard rail passed" in capsys.readouterr().out


def test_more_than_half_flipped_fails(report, capsys):
    report([entry({"A": counts(10, 6)})])
    assert check_daily_diff.main() == 1
    out = capsys.readouterr().out
    assert "GUARD RAIL FAILED" in out
    assert "6/10" in out


# --- the five-record floor ---------------------------------------------------

def test_a_baseline_under_five_is_ignored_however_much_flipped(report):
    """One of two expiring is 50% and means nothing; so does all four of four."""
    report([entry({"A": counts(4, 4)})])
    assert check_daily_diff.main() == 0


def test_a_baseline_of_exactly_five_is_judged(report):
    report([entry({"A": counts(5, 5)})])
    assert check_daily_diff.main() == 1


def test_the_floor_is_the_scrapes_floor():
    import scrape

    assert check_daily_diff.MIN_BASELINE == scrape.ALL_GONE_FLOOR


# --- which entries count ---------------------------------------------------------

def test_every_entity_in_every_entry_is_checked(report, capsys):
    report([entry({"A": counts(100, 1)}, "contract"),
            entry({"B": counts(100, 2), "C": counts(20, 15)}, "state")])
    assert check_daily_diff.main() == 1
    out = capsys.readouterr().out
    assert "1 entity/day combination(s)" in out
    assert "[state @" in out and "C: 15/20 (75%)" in out


def test_an_unfinished_night_is_judged_like_any_other(report, capsys):
    report([entry({"A": counts(10, 6)}, complete=False)])
    assert check_daily_diff.main() == 1
    assert "unfinished night" in capsys.readouterr().out


def test_an_unfinished_night_that_passes_says_so(report, capsys):
    report([entry({"A": counts(10, 1)}, complete=False), entry({"A": counts(10, 1)})])
    assert check_daily_diff.main() == 0
    assert "(1 from a night that did not finish)" in capsys.readouterr().out


def test_a_report_with_no_entities_passes(report):
    report([entry({})])
    assert check_daily_diff.main() == 0


# --- the report is cleared on every exit ---------------------------------------------
#
# The nightly publishes after the guard rail and saves its cache after that. If a
# failing run left the report behind, it would accumulate and the same night's
# violation would fail every later publish for good.

def test_a_passing_run_clears_the_report(report):
    path = report([entry({"A": counts(10, 1)})])
    assert check_daily_diff.main() == 0
    assert not path.exists()


def test_a_failing_run_clears_the_report_too(report):
    path = report([entry({"A": counts(10, 9)})])
    assert check_daily_diff.main() == 1
    assert not path.exists()


def test_no_report_means_nothing_to_check(report, capsys):
    assert check_daily_diff.main() == 0
    assert "No daily diff report" in capsys.readouterr().out
