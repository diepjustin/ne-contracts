"""Stopping a 33-hour run must cost the rows in flight and nothing else.

The backfill is expected to be interrupted -- a closed lid, a moved laptop, a
changed network -- so resume is the normal path through this code, not the
error path. These tests pin the three ways it could quietly lose or invent
data:

  * a resumed run must skip what is already recorded and re-ask the rest
  * a log truncated mid-write by a hard kill must not crash the next run
  * a log broken anywhere *earlier* must stop the run, because something
    rewrote history and the rows after it cannot be trusted

They also pin the rule the whole project is built on: a failed fetch is never
written, so "not recorded" and "not yet reached" stay the same thing.
"""

import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import scrape  # noqa: E402
import backfill_documents as backfill  # noqa: E402


def a_row(doc, detail):
    return {"Document Number": doc, "Entity Name": "Test Agency", "Detail URL": detail}


def documents(*names):
    return [{"name": n, "size": "1Mb", "token": f"tok-{n}"} for n in names]


def write_log(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


# --- load_documents: folding, and surviving a hard kill --------------------

def test_a_rechecked_row_supersedes_its_earlier_entry(tmp_path):
    """Append-only with last-entry-wins. Counting lines instead of folding is
    how this project once reported 23% complete as 40%."""
    path = tmp_path / "documents.jsonl"
    write_log(path, [{"k": "aaa", "doc": "D1", "entity": "E", "n": 1, "seen": "2026-01-01"},
                     {"k": "aaa", "doc": "D1", "entity": "E", "n": 4, "seen": "2026-02-01"}])
    store = scrape.load_documents(str(path))
    assert len(store) == 1
    assert store["aaa"]["n"] == 4


def test_a_log_truncated_mid_write_loses_only_its_last_row(tmp_path, capsys):
    """What a SIGKILL during a write actually leaves behind. The row is simply
    asked again; crashing here would make every hard stop a manual repair."""
    path = tmp_path / "documents.jsonl"
    write_log(path, [{"k": "aaa", "doc": "D1", "entity": "E", "n": 1, "seen": "2026-01-01"},
                     {"k": "bbb", "doc": "D2", "entity": "E", "n": 2, "seen": "2026-01-01"}])
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"k": "ccc", "doc": "D3", "ent')      # killed here

    store = scrape.load_documents(str(path))
    assert sorted(store) == ["aaa", "bbb"]
    assert "ends mid-write" in capsys.readouterr().out


def test_a_torn_line_is_removed_so_the_next_append_is_still_readable(tmp_path):
    """The failure that only shows up if you run the whole sequence.

    Dropping the torn line on read is not enough: the next run appends after
    the fragment, which turns it into a broken line in the *middle* of the
    file, and every load from then on refuses to run. Recovery has to survive
    being used twice."""
    path = tmp_path / "documents.jsonl"
    write_log(path, [{"k": "aaa", "doc": "D1", "entity": "E", "n": 1, "seen": "2026-01-01"}])
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"k": "bbb", "doc": "D2", "ent')          # killed here

    scrape.load_documents(str(path))                        # repairs the file
    scrape.append_documents(
        [{"k": "ccc", "doc": "D3", "entity": "E", "n": 2, "seen": "2026-01-02"}], str(path))

    store = scrape.load_documents(str(path))                # must not raise
    assert sorted(store) == ["aaa", "ccc"]


def test_a_log_broken_before_the_end_halts_the_run(tmp_path):
    """Not the same failure at all. A bad line with good lines after it means
    something rewrote this log rather than appending, so every row past it is
    suspect -- and treating unchecked rows as done is the exact shape of the
    two worst bugs this project has had."""
    path = tmp_path / "documents.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"k": "aaa", "doc": "D1", "entity": "E", "n": 1}) + "\n")
        f.write("{ this is not json\n")
        f.write(json.dumps({"k": "ccc", "doc": "D3", "entity": "E", "n": 1}) + "\n")

    with pytest.raises(SystemExit) as exit_info:
        scrape.load_documents(str(path))
    assert "line 2" in str(exit_info.value)


def test_a_missing_log_is_an_empty_start_not_an_error(tmp_path):
    assert scrape.load_documents(str(tmp_path / "nope.jsonl")) == {}


# --- resuming: ask for what is missing, and only that ----------------------

def drive(monkeypatch, tmp_path, rows, answers, store=None):
    """Run one dataset's backfill over fabricated rows.

    `answers` maps Detail URL -> what the detail fetch returns: a list of
    documents, [] for a record that publishes none, or None for a page we
    could not read."""
    asked = []

    def fake_fetch(records):
        asked.extend(r["detail_url"] for r in records)
        return [answers[r["detail_url"]] for r in records]

    log = tmp_path / "documents.jsonl"
    monkeypatch.setattr(backfill, "fetch_documents_parallel", fake_fetch)
    monkeypatch.setattr(backfill, "rows_for", lambda _d: rows)
    monkeypatch.setattr(backfill, "DOCUMENTS_JSONL", str(log))
    monkeypatch.setattr(backfill, "ROOT", "")
    monkeypatch.setattr(backfill, "drain_latency", lambda: None)

    result = backfill.backfill("contract", store if store is not None else {},
                               {"stopping": False})
    return result, asked, log


def test_a_resumed_run_asks_only_for_what_is_missing(monkeypatch, tmp_path):
    """The whole point of resume. Re-asking recorded rows would turn a 33-hour
    job into an unbounded one every time the laptop moved."""
    rows = [a_row("D1", "url-1"), a_row("D2", "url-2"), a_row("D3", "url-3")]
    store = {scrape.document_key("url-1"): {"k": "x", "n": 1}}
    (recorded, _multi), asked, _log = drive(
        monkeypatch, tmp_path, rows,
        {"url-2": documents("A"), "url-3": documents("A", "B")}, store)

    assert asked == ["url-2", "url-3"]
    assert recorded == 2


def test_everything_recorded_means_nothing_is_asked(monkeypatch, tmp_path):
    rows = [a_row("D1", "url-1")]
    store = {scrape.document_key("url-1"): {"k": "x", "n": 1}}
    (recorded, _multi), asked, _log = drive(monkeypatch, tmp_path, rows, {}, store)
    assert asked == [] and recorded == 0


def test_a_row_we_could_not_read_is_not_recorded(monkeypatch, tmp_path):
    """So it is asked again next run. Writing it would say the state publishes
    no document for a page we never actually read -- the failure that has cost
    this project the most."""
    rows = [a_row("D1", "url-1"), a_row("D2", "url-2")]
    (recorded, _multi), _asked, log = drive(
        monkeypatch, tmp_path, rows, {"url-1": None, "url-2": documents("A")})

    assert recorded == 1
    store = scrape.load_documents(str(log))
    assert scrape.document_key("url-1") not in store
    assert scrape.document_key("url-2") in store


def test_a_record_publishing_nothing_is_recorded_as_a_fact(monkeypatch, tmp_path):
    """Distinct from the row above. We read this page; it offers nothing."""
    rows = [a_row("D1", "url-1")]
    _result, _asked, log = drive(monkeypatch, tmp_path, rows, {"url-1": []})
    assert scrape.load_documents(str(log))[scrape.document_key("url-1")]["n"] == 0


def test_every_document_is_kept_for_a_multi_document_row(monkeypatch, tmp_path):
    rows = [a_row("CW33053", "url-1")]
    _result, _asked, log = drive(
        monkeypatch, tmp_path, rows, {"url-1": documents("A", "B", "C")})

    entry = scrape.load_documents(str(log))[scrape.document_key("url-1")]
    assert entry["n"] == 3
    assert [d["name"] for d in entry["documents"]] == ["A", "B", "C"]
    assert entry["u"] == "url-1"          # multi rows carry the page to open


def test_a_single_document_row_stays_small(monkeypatch, tmp_path):
    """~90% of the corpus. Its one document is already the CSV's View URL, so
    repeating it here would double a 60 MB file for nothing -- but doc and
    entity stay, so the log is still greppable by what a human would search."""
    rows = [a_row("D1", "url-1")]
    _result, _asked, log = drive(monkeypatch, tmp_path, rows, {"url-1": documents("A")})

    entry = scrape.load_documents(str(log))[scrape.document_key("url-1")]
    assert entry["n"] == 1 and "documents" not in entry
    assert entry["doc"] == "D1" and entry["entity"] == "Test Agency"


# --- the breakers ----------------------------------------------------------

def test_a_run_of_failures_halts_the_run(monkeypatch, tmp_path):
    """A closed lid answers every request the same way. Grinding through
    400,000 rows to discover the network is gone wastes the night."""
    rows = [a_row(f"D{i}", f"url-{i}") for i in range(backfill.FAILURE_BREAKER + 5)]
    with pytest.raises(backfill.Stopped) as why:
        drive(monkeypatch, tmp_path, rows, {r["Detail URL"]: None for r in rows})
    assert "could not be fetched" in str(why.value)


def test_a_run_of_empties_re_checks_the_canaries_before_recording(monkeypatch, tmp_path):
    """The 17 Aug 2026 outage in the shape the start-up check cannot catch:
    detail pages served fine with the document links simply gone, fourteen
    hours into a run that began healthy."""
    monkeypatch.setattr(backfill, "document_service_healthy", lambda: False)
    rows = [a_row(f"D{i}", f"url-{i}") for i in range(backfill.EMPTY_BREAKER + 5)]
    with pytest.raises(backfill.Stopped) as why:
        drive(monkeypatch, tmp_path, rows, {r["Detail URL"]: [] for r in rows})
    assert "stopped serving documents" in str(why.value)


def test_real_absences_do_not_trip_the_empty_breaker(monkeypatch, tmp_path):
    """Agencies genuinely do have long runs of records with no document -- 17%
    of state rows. While the canaries answer, those are facts and get recorded."""
    monkeypatch.setattr(backfill, "document_service_healthy", lambda: True)
    rows = [a_row(f"D{i}", f"url-{i}") for i in range(backfill.EMPTY_BREAKER + 5)]
    (recorded, _multi), _asked, _log = drive(
        monkeypatch, tmp_path, rows, {r["Detail URL"]: [] for r in rows})
    assert recorded == len(rows)


def test_asking_to_stop_ends_the_run_cleanly(monkeypatch, tmp_path):
    """Ctrl-C is not an error path. It unwinds to the summary, and the log it
    leaves behind is complete as far as it goes."""
    rows = [a_row("D1", "url-1")]
    monkeypatch.setattr(backfill, "rows_for", lambda _d: rows)
    monkeypatch.setattr(backfill, "DOCUMENTS_JSONL", str(tmp_path / "d.jsonl"))
    monkeypatch.setattr(backfill, "ROOT", "")
    with pytest.raises(backfill.Stopped):
        backfill.backfill("contract", {}, {"stopping": True})


# --- knowing whether a run is going ----------------------------------------
#
# The 28 Aug 2026 sleep killed a run outright, and the only way to tell was to
# go looking for the process by hand. Worse, the run's own log came back with
# NUL bytes and stale disk blocks spliced into it, so grep silently found
# nothing in a file that plainly had content -- the hazard this project already
# documents. --status has to answer from something more trustworthy than a log.

def test_a_live_run_is_reported_with_its_pid(monkeypatch, tmp_path):
    log = tmp_path / "documents.jsonl"
    log.write_text("{}\n")
    monkeypatch.setattr(backfill, "running_elsewhere", lambda: [4242])
    assert "pid 4242" in backfill.describe_run(str(log))


def test_no_run_going_says_how_to_continue(monkeypatch, tmp_path):
    log = tmp_path / "documents.jsonl"
    log.write_text("{}\n")
    monkeypatch.setattr(backfill, "running_elsewhere", lambda: [])
    said = backfill.describe_run(str(log))
    assert "no run is going" in said and "continue" in said


def test_ps_not_answering_is_not_reported_as_nothing_running(monkeypatch, tmp_path):
    """The distinction this whole project turns on. Not being able to look is
    not the same as having looked and found nothing, and a status line that
    conflates them invites someone to start a second run alongside a live one."""
    log = tmp_path / "documents.jsonl"
    log.write_text("{}\n")
    monkeypatch.setattr(backfill, "running_elsewhere", lambda: None)
    said = backfill.describe_run(str(log))
    assert "could not tell" in said
    assert "no run" not in said


def test_a_log_that_does_not_exist_yet_claims_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(backfill, "running_elsewhere", lambda: [])
    assert backfill.describe_run(str(tmp_path / "nope.jsonl")) == \
        "no run has written anything yet"


def test_the_status_check_never_counts_itself_as_a_run():
    """Asking whether a run is going must not answer "yes, you"."""
    found = backfill.running_elsewhere()
    assert found is None or os.getpid() not in found


def test_a_shell_that_merely_names_the_script_is_not_a_run(monkeypatch):
    """How the false positive got in. Launching `python backfill_documents.py
    --status` from a shell gives that *shell* a command line containing the
    script name, so matching on the name alone reported a run going while the
    same line said it had last written four hours earlier. A run is a Python
    process, not anything that mentions the file."""
    fake_ps = (
        "  501 /bin/sh -c cd /repo && ./venv/bin/python scripts/backfill_documents.py --status\n"
        "  502 grep --color=auto backfill_documents.py\n"
        "  503 /repo/venv/bin/python scripts/backfill_documents.py --dataset state\n"
        "  504 /usr/bin/vim scripts/backfill_documents.py\n"
    )

    class Done:
        stdout = fake_ps

    monkeypatch.setattr(backfill.subprocess, "run", lambda *a, **k: Done())
    assert backfill.running_elsewhere() == [503]


def test_ps_failing_is_reported_as_unknown_not_as_absence(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("ps not found")

    monkeypatch.setattr(backfill.subprocess, "run", boom)
    assert backfill.running_elsewhere() is None
