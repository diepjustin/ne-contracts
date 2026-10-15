"""State files that start fresh when unreadable must say so, and keep what they dropped.

scrape_meta.json holds the three datasets' "last scraped" stamps. If it is
corrupt, record_scrape_time starts from {} -- the right call, since the scrape
that has just succeeded must not fail over a stamp -- but silently, so the other
datasets' stamps vanished and the page's oldest-stamp rule could no longer show
that one of them was stale. The diff report did the same to a night's unreviewed
flips. Both now print a GitHub annotation and keep a copy for a person; the
progress files, which are cheap to rebuild, take the warning only. And the stamp
is written beside and renamed over, so a kill mid-write cannot leave it empty.
"""

import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import scrape  # noqa: E402

GOOD = {"contract": "2026-10-12T03:07:01+00:00", "purchase-order": "2026-10-12T03:08:00+00:00",
        "state": "2026-09-24T03:07:01+00:00"}


@pytest.fixture
def meta(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "SCRAPE_META", str(tmp_path / "scrape_meta.json"))
    return tmp_path / "scrape_meta.json"


@pytest.fixture
def report(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "DAILY_DIFF_REPORT", str(tmp_path / "daily_diff_report.json"))
    return tmp_path / "daily_diff_report.json"


def warnings_in(out):
    return [l for l in out.splitlines() if l.startswith("::warning")]


# --- the stamp file ----------------------------------------------------------------

def test_a_corrupt_stamp_file_is_reported_kept_and_started_fresh(meta, capsys):
    meta.write_text('{"contract": "2026-10-12T03:07:01+00:00", "stat', encoding="utf-8")

    stamp = scrape.record_scrape_time("state")

    assert json.loads(meta.read_text(encoding="utf-8")) == {"state": stamp}
    [warning] = warnings_in(capsys.readouterr().out)
    assert str(meta) in warning and "starting it fresh" in warning and ".corrupt" in warning
    assert (meta.parent / "scrape_meta.json.corrupt").read_text(encoding="utf-8").startswith('{"contract"')


@pytest.mark.parametrize("content", ["[]", '"a string"', "42", "null"])
def test_a_stamp_file_that_is_not_an_object_counts_as_corrupt(meta, capsys, content):
    meta.write_text(content, encoding="utf-8")
    stamp = scrape.record_scrape_time("contract")
    assert json.loads(meta.read_text(encoding="utf-8")) == {"contract": stamp}
    assert len(warnings_in(capsys.readouterr().out)) == 1


def test_a_readable_stamp_file_is_quiet_and_keeps_every_other_stamp(meta, capsys):
    meta.write_text(json.dumps(GOOD), encoding="utf-8")

    stamp = scrape.record_scrape_time("contract")

    assert json.loads(meta.read_text(encoding="utf-8")) == dict(GOOD, contract=stamp)
    assert capsys.readouterr().out == ""
    assert not (meta.parent / "scrape_meta.json.corrupt").exists()


def test_a_missing_stamp_file_is_quiet(meta, capsys):
    stamp = scrape.record_scrape_time("contract")
    assert json.loads(meta.read_text(encoding="utf-8")) == {"contract": stamp}
    assert capsys.readouterr().out == ""


def test_the_stamp_is_written_beside_and_renamed_over(meta, monkeypatch):
    """A kill between the write and the rename leaves yesterday's file intact,
    not an empty one for 'Last updated' to be read from."""
    meta.write_text(json.dumps(GOOD), encoding="utf-8")

    def killed(src, dst):
        raise KeyboardInterrupt("killed before the rename")

    monkeypatch.setattr(scrape.os, "replace", killed)
    with pytest.raises(KeyboardInterrupt):
        scrape.record_scrape_time("contract")

    assert json.loads(meta.read_text(encoding="utf-8")) == GOOD


def test_no_temporary_file_is_left_after_a_stamp(meta):
    scrape.record_scrape_time("contract")
    assert sorted(os.listdir(meta.parent)) == ["scrape_meta.json"]


# --- the diff report -----------------------------------------------------------------

COUNTS = {"previously_active": 10, "still_active": 4, "newly_active": 0, "flipped_to_expired": 6}


def test_a_corrupt_diff_report_is_reported_and_kept_before_it_is_restarted(report, capsys):
    report.write_text('[{"dataset": "state", "timestamp": "2026-10-13T00:00:00-05:00", "entit',
                      encoding="utf-8")

    scrape.append_daily_diff_report("contract", {"A": COUNTS}, "2026-10-14T00:00:00-05:00")

    [entry] = json.loads(report.read_text(encoding="utf-8"))
    assert entry["dataset"] == "contract"
    [warning] = warnings_in(capsys.readouterr().out)
    assert "starting it fresh" in warning
    assert (report.parent / "daily_diff_report.json.corrupt").read_text(encoding="utf-8").startswith('[{"dataset": "state"')


def test_a_diff_report_that_is_not_a_list_counts_as_corrupt(report, capsys):
    report.write_text("{}", encoding="utf-8")
    scrape.append_daily_diff_report("contract", {"A": COUNTS}, "2026-10-14T00:00:00-05:00")
    assert len(json.loads(report.read_text(encoding="utf-8"))) == 1
    assert len(warnings_in(capsys.readouterr().out)) == 1


def test_a_readable_diff_report_is_quiet(report, capsys):
    scrape.append_daily_diff_report("contract", {"A": COUNTS}, "2026-10-14T00:00:00-05:00")
    scrape.append_daily_diff_report("state", {"A": COUNTS}, "2026-10-14T00:10:00-05:00")
    assert len(json.loads(report.read_text(encoding="utf-8"))) == 2
    assert capsys.readouterr().out == ""


# --- the progress files: the full scrape's is kept, --daily's is not --------------------------

def test_a_corrupt_full_scrape_checkpoint_says_it_is_starting_over_and_keeps_a_copy(tmp_path, capsys):
    """It is the only record of which CSV rows are legitimate, and the first checkpoint
    overwrites it: a copy is what a person gets to look at."""
    path = tmp_path / "contract_scrape_progress.json"
    path.write_text('{"done": [["A", "Act', encoding="utf-8")

    assert scrape.load_progress(str(path)) == (set(), {})

    [warning] = warnings_in(capsys.readouterr().out)
    assert str(path) in warning and "starting it fresh" in warning
    assert f"A copy is kept as {path}.corrupt." in warning
    assert (tmp_path / "contract_scrape_progress.json.corrupt").read_text(encoding="utf-8") == '{"done": [["A", "Act'


def test_a_copy_that_cannot_be_made_is_not_claimed(tmp_path, monkeypatch, capsys):
    path = tmp_path / "state.json"
    path.write_text("x", encoding="utf-8")

    def refuse(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(scrape.shutil, "copyfile", refuse)
    scrape.set_aside_unreadable(str(path), "bad")

    [warning] = capsys.readouterr().out.splitlines()
    assert "A copy is kept" not in warning
    assert "A copy could not be kept (PermissionError: [Errno 13] Permission denied)." in warning
    assert not (tmp_path / "state.json.corrupt").exists()


def test_a_corrupt_daily_progress_file_says_so(tmp_path, capsys):
    path = tmp_path / "contract_daily_progress.json"
    path.write_text("not json", encoding="utf-8")

    assert scrape.load_daily_progress(str(path)) == set()

    assert len(warnings_in(capsys.readouterr().out)) == 1
    assert not (tmp_path / "contract_daily_progress.json.corrupt").exists()


def test_missing_progress_files_are_not_news(tmp_path, capsys):
    assert scrape.load_progress(str(tmp_path / "none.json")) == (set(), {})
    assert scrape.load_daily_progress(str(tmp_path / "none.json")) == set()
    assert capsys.readouterr().out == ""


def test_the_annotation_is_one_line_however_many_the_error_has(tmp_path, capsys):
    path = tmp_path / "state.json"
    path.write_text("x", encoding="utf-8")
    scrape.set_aside_unreadable(str(path), "first line\nsecond line")
    [warning] = capsys.readouterr().out.splitlines()
    assert warning.startswith("::warning") and "first line second line" in warning
