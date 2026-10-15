"""check_scrape_stamps.py: an unfinished night has to turn the run red.

scrape.py stamps a dataset only when every entity in it was read, and a night that does not
finish leaves the stamp where it was, exits 0 and ends green. The 24 Sep 2026 freeze went 19
nights that way before a person noticed the page's date had not moved. This check compares
each stamp with the moment the run started. Pure file-in, exit-code-out tests; nothing here
touches the network or data/.
"""

import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import build_site  # noqa: E402
import check_scrape_stamps  # noqa: E402
import scrape  # noqa: E402

SINCE = "2026-10-14T08:00:02+00:00"          # 03:00:02 Central, as the workflow records it
FRESH = "2026-10-14T03:12:44-05:00"          # 08:12:44 UTC: twelve minutes into the run
TITLE = "::error title=Daily scrape did not finish::"


def stamps(**override):
    out = {"contract": FRESH, "purchase-order": FRESH, "state": FRESH}
    out.update(override)
    return out


@pytest.fixture
def check(tmp_path, capsys):
    """check(content, since=SINCE) -> (exit code, output lines). content is a dict, or raw text."""
    def run(content, since=SINCE, raw=False):
        path = tmp_path / "scrape_meta.json"
        if content is not None:
            path.write_text(content if raw else json.dumps(content), encoding="utf-8")
        code = check_scrape_stamps.main(["--since", since, "--meta", str(path)])
        return code, capsys.readouterr().out.splitlines()
    return run


def errors(lines):
    return [line[len(TITLE):] for line in lines if line.startswith(TITLE)]


# --- the nights that matter ---------------------------------------------------------------------

def test_a_night_where_all_three_datasets_finished_is_green(check):
    code, lines = check(stamps())
    assert code == 0 and errors(lines) == []
    assert lines[-1].startswith("All 3 datasets finished since this run started at 2026-10-14T08:00:02+00:00")
    assert [line.split(":")[0] for line in lines[:3]] == ["contract", "purchase-order", "state"]


def test_the_24_sep_freeze_is_red_and_names_only_the_dataset_that_froze(check):
    """contract and purchase-order finished; state's stamp never moved."""
    code, lines = check(stamps(state="2026-09-24T03:11:02-05:00"))
    assert code == 1
    (problem,) = errors(lines)
    assert problem.startswith("state: last finished 2026-09-24T03:11:02-05:00, before this run started")
    assert "did not complete" in problem


def test_a_night_where_nothing_finished_names_all_three(check):
    old = "2026-10-13T03:10:00-05:00"
    code, lines = check(stamps(contract=old, **{"purchase-order": old}, state=old))
    assert code == 1
    assert [e.split(":")[0] for e in errors(lines)] == ["contract", "purchase-order", "state"]


# --- the boundary and the clocks --------------------------------------------------------------------

def test_a_stamp_at_the_moment_the_run_started_counts(check):
    assert check(stamps(state="2026-10-14T08:00:02+00:00"))[0] == 0


def test_a_stamp_one_second_before_the_run_started_does_not(check):
    code, lines = check(stamps(state="2026-10-14T08:00:01+00:00"))
    assert code == 1 and errors(lines)[0].startswith("state: last finished 2026-10-14T08:00:01+00:00")


@pytest.mark.parametrize("stamp, ok", [
    ("2026-10-14T03:00:02-05:00", True),     # the very instant of SINCE, written in Central
    ("2026-10-14T03:00:01-05:00", False),    # one second earlier
    ("2026-10-14T09:30:00+01:00", True),     # 08:30 UTC, after
    ("2026-10-14T08:59:00+01:00", False),    # 07:59 UTC, before: the clock reading is later, the instant is not
    ("2026-10-14T09:01:00+01:00", True),     # 08:01 UTC, after
    ("2026-10-14T08:12:44Z", True),          # a trailing Z, which fromisoformat only took from 3.11
    ("2026-10-14T07:59:59Z", False),
])
def test_stamps_are_compared_as_instants_whatever_offset_they_were_written_in(check, stamp, ok):
    assert (check(stamps(state=stamp))[0] == 0) is ok


def test_since_may_be_written_in_any_offset_too(check):
    assert check(stamps(), since="2026-10-14T03:00:02-05:00")[0] == 0
    assert check(stamps(), since="2026-10-14T03:30:00-05:00")[0] == 1       # 08:30 UTC, after the stamps
    assert check(stamps(), since="2026-10-14T08:00:02Z")[0] == 0


# --- stamps it cannot use are problems, not crashes ---------------------------------------------------

def test_a_missing_dataset_is_named(check):
    code, lines = check({"contract": FRESH, "purchase-order": FRESH})
    assert code == 1
    (problem,) = errors(lines)
    assert problem.startswith("state: no stamp")


@pytest.mark.parametrize("content, why", [
    (None, "scrape_meta.json could not be read (FileNotFoundError"),
    ("", "scrape_meta.json could not be read (JSONDecodeError"),
    ("{not json", "scrape_meta.json could not be read (JSONDecodeError"),
    ("[]", "scrape_meta.json holds list, not an object"),
    ('"2026-10-14"', "scrape_meta.json holds str, not an object"),
    ("null", "scrape_meta.json holds NoneType, not an object"),
])
def test_an_unreadable_file_fails_every_dataset_and_says_why(check, content, why):
    code, lines = check(content, raw=True)
    assert code == 1
    found = errors(lines)
    assert [e.split(":")[0] for e in found] == ["contract", "purchase-order", "state"]
    assert all(f"no stamp: {why}" in e for e in found)


def test_a_directory_where_the_file_should_be_is_reported_not_raised(tmp_path, capsys):
    code = check_scrape_stamps.main(["--since", SINCE, "--meta", str(tmp_path)])
    assert code == 1
    assert "no stamp: " in capsys.readouterr().out


@pytest.mark.parametrize("bad", [123, None, ["2026-10-14T08:12:44+00:00"], {"at": FRESH}, "", "yesterday",
                                 "2026-13-45T00:00:00+00:00"])
def test_a_stamp_that_is_not_a_timestamp_is_a_problem(check, bad):
    code, lines = check(stamps(state=bad))
    assert code == 1
    (problem,) = errors(lines)
    assert problem.startswith("state: its stamp") and "is not usable" in problem


@pytest.mark.parametrize("naive", ["2026-10-14T03:12:44", "2026-10-14", "2026-10-14 03:12:44"])
def test_a_stamp_with_no_utc_offset_cannot_be_compared_and_is_a_problem(check, naive):
    """scrape.py always writes an offset. A bare local time compared as UTC would call a fresh
    night stale by the Central offset, or a stale one fresh, so it is refused instead of guessed."""
    code, lines = check(stamps(state=naive))
    assert code == 1 and "no UTC offset" in errors(lines)[0]


@pytest.mark.parametrize("since", ["", "tonight", "2026-10-14T08:00:02", "2026-10-14"])
def test_a_since_that_cannot_be_used_fails_the_step_and_says_so(check, since):
    code, lines = check(stamps(), since=since)
    assert code == 1
    (problem,) = errors(lines)
    assert problem.startswith(f"--since {since!r} is not usable")


# --- the output cannot be bent into a workflow command -----------------------------------------------------

def test_escaping_follows_githubs_rules_for_a_command_message():
    assert check_scrape_stamps.escape("a%b\r\nc") == "a%25b%0D%0Ac"


def test_data_in_the_stamps_file_cannot_start_a_workflow_command(check):
    """A parseable-but-stale stamp keeps its raw text in the message, trailing newline and all;
    an unparseable one is shown through repr(). Neither may put a second command on a line of its own."""
    code, lines = check(stamps(state="2026-09-24T03:00:00-05:00\n",
                               contract="x\n::warning::injected\r::stop-commands::z"))
    assert code == 1
    assert all(line.startswith(TITLE) or not line.startswith("::") for line in lines)
    assert len(errors(lines)) == 2 and sum(1 for line in lines if line.startswith("::")) == 2
    assert any("%0A" in line for line in lines), "the newline that survived the strip is escaped"


# --- it never raises ------------------------------------------------------------------------------------

def test_a_failure_of_the_check_itself_is_reported_and_red(check, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(check_scrape_stamps, "problems", boom)
    code, lines = check(stamps())
    assert code == 1
    assert errors(lines) == ["check_scrape_stamps.py itself failed: RuntimeError: something unexpected"]


# --- it agrees with the code that writes and reads the stamps -----------------------------------------------

def test_the_datasets_are_the_ones_scrape_stamps_and_the_page_reads():
    assert set(check_scrape_stamps.DATASETS) == set(scrape.DATASETS) == set(build_site.DATA)
    assert len(check_scrape_stamps.DATASETS) == 3


def test_stamps_written_by_scrape_py_are_read_back_correctly(tmp_path, monkeypatch, capsys):
    path = tmp_path / "scrape_meta.json"
    monkeypatch.setattr(scrape, "SCRAPE_META", str(path))
    for dataset in check_scrape_stamps.DATASETS:
        scrape.record_scrape_time(dataset)

    before = "2000-01-01T00:00:00+00:00"
    after = "2999-01-01T00:00:00+00:00"
    assert check_scrape_stamps.main(["--since", before, "--meta", str(path)]) == 0
    assert check_scrape_stamps.main(["--since", after, "--meta", str(path)]) == 1
    capsys.readouterr()


def test_the_default_file_is_the_one_scrape_py_and_the_page_use():
    assert check_scrape_stamps.META == os.path.abspath(os.path.join(SCRIPTS, "..", scrape.SCRAPE_META))
    assert check_scrape_stamps.META == build_site.SCRAPE_META
