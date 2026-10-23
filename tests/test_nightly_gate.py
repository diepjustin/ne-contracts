"""nightly_gate.py: the backup cron entry skips a finished night and never skips an unfinished one.

The times are real shapes from the Oct 2026 runs: entries at 03:17 and 04:47 UTC that start
4-5 hours late, one 11.8-hour outlier, and the two DST changes the old "which entry fired"
gate got wrong (sc-03, ci-07). Pure file-in, exit-code-out; no network.
"""

import datetime
import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import nightly_gate  # noqa: E402

UTC = datetime.timezone.utc


def at(text):
    return datetime.datetime.fromisoformat(text)


def write(tmp_path, stamp=None, **override):
    data = {"contract": stamp, "purchase-order": stamp, "state": stamp}
    data.update(override)
    path = tmp_path / "scrape_meta.json"
    path.write_text(json.dumps({k: v for k, v in data.items() if v is not None}))
    return str(path)


def gate(tmp_path, now, stamp=None, **override):
    return nightly_gate.main(["--meta", write(tmp_path, stamp, **override), "--now", now])


@pytest.mark.parametrize("now, noon", [
    ("2026-10-23T08:00:00+00:00", "2026-10-22T12:00:00-05:00"),   # 03:00 CDT: last night's noon
    ("2026-10-23T17:00:00+00:00", "2026-10-23T12:00:00-05:00"),   # exactly noon CDT
    ("2026-10-23T16:59:59+00:00", "2026-10-22T12:00:00-05:00"),   # a second before it
    ("2026-10-24T03:17:00+00:00", "2026-10-23T12:00:00-05:00"),   # an on-time 22:17 CDT run
    ("2026-11-01T09:00:00+00:00", "2026-10-31T12:00:00-05:00"),   # 03:00 CST, just after fall-back
    ("2026-11-02T09:00:00+00:00", "2026-11-01T12:00:00-06:00"),   # the first CST night
    ("2027-03-14T08:30:00+00:00", "2027-03-13T12:00:00-06:00"),   # 03:30 CDT, just after spring-forward
    ("2027-03-15T08:30:00+00:00", "2027-03-14T12:00:00-05:00"),
])
def test_a_night_starts_at_the_last_chicago_noon(now, noon):
    assert nightly_gate.night_start(at(now)) == at(noon)


def test_the_backup_entry_skips_a_night_the_first_entry_finished(tmp_path):
    # 03:17 UTC entry started 4h40m late and finished 16 minutes later; the 04:47 one starts 4h50m late.
    assert gate(tmp_path, "2026-10-23T09:37:00+00:00", "2026-10-23T08:13:30+00:00") == 0


def test_the_next_nights_first_entry_runs(tmp_path):
    assert gate(tmp_path, "2026-10-24T08:01:00+00:00", "2026-10-23T08:13:30+00:00") == 1


def test_an_on_time_run_after_a_late_one_is_not_mistaken_for_the_same_night(tmp_path):
    """The trap in "skip if today's Chicago release exists": the 3 am scrape on the 23rd
    makes a release dated the 23rd, and the on-time 10:17 pm run that same Chicago day is
    the next night's scrape, not a repeat."""
    assert gate(tmp_path, "2026-10-24T03:17:05+00:00", "2026-10-23T08:13:30+00:00") == 1


def test_the_worst_observed_delay_still_lands_in_its_own_night(tmp_path):
    # 11.8 h late: the 03:17 UTC entry starts 15:05 UTC (10:05 CDT) and finishes 15:21.
    finished = "2026-10-23T15:21:00+00:00"
    assert gate(tmp_path, "2026-10-23T16:40:00+00:00", finished) == 0     # its backup skips
    assert gate(tmp_path, "2026-10-24T03:17:00+00:00", finished) == 1     # next night, on time, runs


@pytest.mark.parametrize("now, finished", [
    ("2027-03-14T09:30:00+00:00", "2027-03-14T08:05:00+00:00"),   # spring-forward night
    ("2026-11-01T09:30:00+00:00", "2026-11-01T08:40:00+00:00"),   # fall-back night
])
def test_dst_changes_do_not_move_the_night(tmp_path, now, finished):
    assert gate(tmp_path, now, finished) == 0


def test_one_unfinished_dataset_means_the_backup_runs(tmp_path, capsys):
    rc = gate(tmp_path, "2026-10-23T09:37:00+00:00", "2026-10-23T08:13:30+00:00",
              state="2026-10-22T08:15:00+00:00")
    assert rc == 1
    out = capsys.readouterr().out
    assert "state:" in out and "::error" not in out, "a night still to scrape is not an error"


def test_a_missing_dataset_means_run(tmp_path):
    assert gate(tmp_path, "2026-10-23T09:37:00+00:00", "2026-10-23T08:13:30+00:00", contract=None) == 1


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"contract": 5}'])
def test_an_unusable_file_means_run(tmp_path, content):
    path = tmp_path / "scrape_meta.json"
    path.write_text(content)
    assert nightly_gate.main(["--meta", str(path), "--now", "2026-10-23T09:37:00+00:00"]) != 0


def test_no_file_means_run(tmp_path):
    assert nightly_gate.main(["--meta", str(tmp_path / "absent.json"), "--now", "2026-10-23T09:37:00+00:00"]) != 0


def test_a_failure_of_the_gate_itself_means_run(tmp_path, monkeypatch):
    def boom(now):
        raise RuntimeError("no tz database")
    monkeypatch.setattr(nightly_gate, "night_start", boom)
    assert gate(tmp_path, "2026-10-23T09:37:00+00:00", "2026-10-23T08:13:30+00:00") == 2


def test_without_now_it_reads_the_clock(tmp_path):
    fresh = datetime.datetime.now(UTC).isoformat()
    assert nightly_gate.main(["--meta", write(tmp_path, fresh)]) == 0
