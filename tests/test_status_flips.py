"""Deciding a record's Status from one night's Active search.

On 17-18 Aug 2026 the state's search returned "No results found" for every
entity for two days. `scrape_entity` treats an empty first page as a clean
finish, so `--daily` saw zero active records, computed `known - seen` as
everything, and marked all 44,063 active records in the database Expired in a
single run. Every step succeeded; the run was green. It could not repair itself
either -- `--daily` only ever flips Active to Expired, so the damage was
permanent until the data was rebuilt by hand.

The refusal added for that had no floor, and it froze the other way. From 25
Sep 2026 an agency with one active record that genuinely ended was refused
every night, the state dataset never finished, its scrape time stuck at 24 Sep,
and the flips patched on those nights reached no report the guard rail reads.

These tests pin that a flip is evidence of an ending rather than of an absence,
that ordinary and small endings still happen, and that every patched flip
reaches the guard rail.
"""

import argparse
import csv
import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import check_daily_diff  # noqa: E402
import scrape  # noqa: E402


def urls(*names):
    return {f"https://example.test/detail?D={n}" for n in names}


# --- the refusal itself -----------------------------------------------------

def test_an_empty_answer_never_expires_anything():
    """The 17 Aug shape: the entity had active contracts and returned nothing."""
    assert scrape.refuse_flips(urls(*range(50)), set())


def test_a_wholesale_disappearance_is_refused():
    """A partial outage: most records missing at once is not 90 endings."""
    assert scrape.refuse_flips(urls(*range(100)), urls(*range(10)))


def test_an_ordinary_expiry_still_flips():
    """The guard must not freeze the database: a few contracts ending is normal
    and is the entire point of --daily."""
    assert scrape.refuse_flips(urls(*range(100)), urls(*range(3, 100))) is None


def test_a_small_entity_losing_most_of_its_records_is_still_allowed():
    assert scrape.refuse_flips(urls(*range(4)), urls(0)) is None


@pytest.mark.parametrize("active", [1, 4])
def test_a_small_entity_losing_everything_is_still_allowed(active):
    """Below the floor, a genuine clear-out must not be blocked -- a body with
    four contracts can legitimately end all four, and one with a single
    contract ending it is what froze the state dataset for four nights."""
    assert scrape.refuse_flips(urls(*range(active)), set()) is None


def test_five_active_records_all_gone_is_refused():
    assert scrape.refuse_flips(urls(*range(5)), set())


def test_nothing_known_means_nothing_to_flip():
    assert scrape.refuse_flips(set(), set()) is None


def test_the_floor_is_the_guard_rails_baseline():
    """Raised alone, the scrape would flip small clear-outs the guard rail then
    fails the night for; lowered alone, a small body's last contract ending
    would freeze the dataset again."""
    assert scrape.ALL_GONE_FLOOR == check_daily_diff.MIN_BASELINE


# --- whole nights, against a scripted Active search ---------------------------

CAMPUS = "Chadron State College"
OTHER = "Peru State College"
OLD_VIEW = "https://example.test/view?D=first"
NEW_VIEW = "https://example.test/view?D=fetched-tonight"
# What a record flipped back is given: run_daily fetches its detail page itself, through
# fetch_documents_parallel, which night() answers with this one document.
RESTORED_DOCUMENTS = [{"name": "DOC", "size": "1Mb", "token": "restored-tonight"}]
RESTORED_VIEW = scrape.view_url_for(RESTORED_DOCUMENTS)


def row(n, entity=CAMPUS, status="Active", amount="$100.00"):
    return [str(n), "Contract", "050", entity, "ACME SUPPLY", amount, "07/01/2026",
            "06/30/2027", status, f"https://example.test/detail?D={n}", OLD_VIEW]


def listed(n, entity=CAMPUS, amount="$100.00"):
    """A record as the search grid reports it -- entity name shouted, as state
    grids do, while the CSV holds the canonical name."""
    return {"doc_number": str(n), "doc_type": "Contract", "entity_code": "050",
            "entity_name": entity.upper(), "vendor": "ACME SUPPLY", "amount": amount,
            "begin_date": "07/01/2026", "end_date": "06/30/2027",
            "detail_url": f"https://example.test/detail?D={n}"}


class NoNetwork:
    def __init__(self, *a, **k):
        raise AssertionError("a test reached for the network")


def night(monkeypatch, tmp_path, rows, search, entities=(CAMPUS, OTHER), only=None,
          documents=None, today=None, accept_strays=False):
    """One `scrape.py contract --daily` over `rows`, the search answering `search`.

    `entities` is the whole dataset (it stands in for HIGHER_ED_ENTITIES, so a night
    that reads all of them is complete); `only` is an `--entity` subset, if any.
    `documents` answers run_daily's own detail fetches, {detail_url: list or None},
    RESTORED_DOCUMENTS for any URL it does not name. Every documents.jsonl entry the
    night writes is collected in night.documents. `today` (a datetime.date) is the date
    the night runs on, for tests that span several; it is the real date otherwise.

    The fake honours scrape_entity's contract, including asking record_filter
    twice per record -- once to choose what to fetch, once before writing.
    Rows are only written on the first call; later calls reuse the CSV.
    """
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    csv_path = data / "nu_contracts.csv"
    if rows is not None:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([scrape.CSV_HEADER] + rows)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scrape, "document_service_healthy", lambda: True)
    monkeypatch.setattr(scrape, "build_session", lambda: None)
    # Nothing here may reach the state: a detail fetch that got past the fakes fails the test.
    monkeypatch.setattr(scrape, "detail_session", NoNetwork)
    monkeypatch.setattr(scrape.requests, "Session", NoNetwork)
    answers = documents or {}
    monkeypatch.setattr(scrape, "fetch_documents_parallel",
                        lambda records: [answers.get(r["detail_url"], RESTORED_DOCUMENTS) for r in records])
    night.documents = []
    monkeypatch.setattr(scrape, "append_documents",
                        lambda entries, path=None: night.documents.extend(entries) or len(entries))

    def fake_scrape_entity(session, entity_name, entity_val, status, entity_type, doc_type,
                           writer, deadline=None, record_filter=None, on_record_seen=None, **_):
        records = search.get(entity_name, [])
        to_fetch = {r["detail_url"] for r in records if record_filter(r)}
        for r in records:
            on_record_seen(r)
            if record_filter(r) and r["detail_url"] in to_fetch:
                writer.writerow([r["doc_number"], r["doc_type"], r["entity_code"], entity_name,
                                 r["vendor"], r["amount"], r["begin_date"], r["end_date"],
                                 status, r["detail_url"], NEW_VIEW])
        return len(records), True

    monkeypatch.setattr(scrape, "scrape_entity", fake_scrape_entity)
    monkeypatch.setattr(scrape, "HIGHER_ED_ENTITIES", {e: e for e in entities})
    if today is not None:
        import datetime
        import types

        class Day(datetime.date):
            @classmethod
            def today(cls):
                return today
        monkeypatch.setattr(scrape, "datetime", types.SimpleNamespace(
            date=Day, datetime=datetime.datetime, timedelta=datetime.timedelta))
    scrape.run_daily(argparse.Namespace(dataset="contract", entity=list(only) if only else None,
                                        hours=None, accept_strays=accept_strays))
    monkeypatch.setattr(scrape, "datetime", __import__("datetime"))

    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))[1:]


def report(tmp_path):
    path = tmp_path / "data" / "daily_diff_report.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def stamped(tmp_path):
    path = tmp_path / "data" / "scrape_meta.json"
    return path.exists() and "contract" in json.loads(path.read_text(encoding="utf-8"))


def status_of(rows, n):
    return [r[8] for r in rows if r[0] == str(n)]


@pytest.mark.parametrize("active", [1, 4])
def test_a_small_body_ending_everything_flips_and_the_night_finishes(monkeypatch, tmp_path, active):
    rows = [row(n) for n in range(active)] + [row(99, OTHER)]
    out = night(monkeypatch, tmp_path, rows, {OTHER: [listed(99, OTHER)]})

    assert all(status_of(out, n) == ["Expired"] for n in range(active))
    assert stamped(tmp_path)
    [entry] = report(tmp_path)
    assert entry["complete"] is True
    assert entry["entities"][CAMPUS]["flipped_to_expired"] == active


def test_five_all_gone_is_refused_and_holds_the_night_open(monkeypatch, tmp_path):
    rows = [row(n) for n in range(5)] + [row(99, OTHER)]
    out = night(monkeypatch, tmp_path, rows, {OTHER: [listed(99, OTHER)]})

    assert all(status_of(out, n) == ["Active"] for n in range(5))
    assert not stamped(tmp_path)
    [entry] = report(tmp_path)
    assert CAMPUS not in entry["entities"]


def test_an_unfinished_night_still_reports_what_it_patched(monkeypatch, tmp_path, capsys):
    """Six of ten gone is under the scrape's 20-record refusal but over the guard
    rail's half, and OTHER is refused, so the night never finishes. The six
    flips are patched regardless -- so the guard rail has to see them."""
    rows = [row(n) for n in range(10)] + [row(90 + n, OTHER) for n in range(5)]
    out = night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(4)]})

    assert sum(status_of(out, n) == ["Expired"] for n in range(10)) == 6
    assert not stamped(tmp_path), "a night that did not finish must not claim freshness"
    [entry] = report(tmp_path)
    assert entry["complete"] is False
    assert entry["timestamp"]
    assert entry["entities"] == {CAMPUS: {"previously_active": 10, "still_active": 4,
                                          "newly_active": 0, "held_back": 0,
                                          "flipped_to_expired": 6, "flipped_back": 0}}

    monkeypatch.setattr(check_daily_diff, "REPORT", str(tmp_path / "data" / "daily_diff_report.json"))
    assert check_daily_diff.main() == 1
    assert "unfinished night" in capsys.readouterr().out


def test_a_same_day_rerun_reports_each_entity_once(monkeypatch, tmp_path):
    rows = [row(n) for n in range(10)] + [row(90 + n, OTHER) for n in range(5)]
    search = {CAMPUS: [listed(n) for n in range(9)]}
    night(monkeypatch, tmp_path, rows, search)
    night(monkeypatch, tmp_path, None, search)       # OTHER refused again

    [entry] = report(tmp_path)
    assert entry["complete"] is False
    assert entry["entities"][CAMPUS]["flipped_to_expired"] == 1

    search[OTHER] = [listed(90 + n, OTHER) for n in range(5)]
    night(monkeypatch, tmp_path, None, search)       # the state answers; the night finishes

    [entry] = report(tmp_path)
    assert entry["complete"] is True
    assert set(entry["entities"]) == {CAMPUS, OTHER}
    assert entry["entities"][CAMPUS]["flipped_to_expired"] == 1
    assert entry["totals"]["previously_active"] == 15
    assert stamped(tmp_path)


# --- the dataset-wide circuit breaker -----------------------------------------
#
# refuse_flips judges one entity at a time and lets an entity of fewer than five active
# records lose all of them. On a night the state answers empty for everything, the large
# entities are refused and the small ones flip to Expired -- 17 of them, 29 records, on the
# 28 Sep 2026 release -- with nothing to mark the night bad, and nothing restores them.

def test_an_outage_night_flips_nothing_for_any_entity(monkeypatch, tmp_path, capsys):
    rows = [row(n) for n in range(60)] + [row(900 + n, OTHER) for n in range(3)]
    out = night(monkeypatch, tmp_path, rows, {})          # the state answers empty for both

    assert all(r[8] == "Active" for r in out), "the small entity's three records must not flip either"
    assert not stamped(tmp_path)
    assert report(tmp_path) == [], "nothing flipped, so the guard rail has nothing to see"
    printed = capsys.readouterr().out
    assert "::error title=Daily scrape: the state looks like it is answering empty::contract:" in printed
    assert "63 of 63 active records (100%) vanished across the dataset" in printed
    assert "small ones included" in printed


def test_without_an_outage_the_small_entity_still_flips(monkeypatch, tmp_path, capsys):
    """The same three records, on a night the large entity answers normally."""
    rows = [row(n) for n in range(60)] + [row(900 + n, OTHER) for n in range(3)]
    out = night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(60)]})

    assert all(status_of(out, 900 + n) == ["Expired"] for n in range(3))
    assert stamped(tmp_path)
    assert "answering empty" not in capsys.readouterr().out


@pytest.mark.parametrize("big, small, trips", [
    (45, 4, False),    # 49 known active: under the floor, a share of so few means nothing
    (46, 4, True),     # 50 known active, all gone
])
def test_the_floor_is_fifty_known_active_records(monkeypatch, tmp_path, big, small, trips):
    rows = [row(n) for n in range(big)] + [row(900 + n, OTHER) for n in range(small)]
    out = night(monkeypatch, tmp_path, rows, {})
    flipped = [status_of(out, 900 + n) == ["Expired"] for n in range(small)]
    assert flipped == [not trips] * small      # the small entity's whole stock flips, or none of it does


@pytest.mark.parametrize("gone_in_campus, trips", [
    (30, False),   # 50 of 100 vanished: exactly half is not more than half...
    (31, True),    # ...51 of 100 is. CAMPUS is then refused on its own account (31 of 60), and OTHER's flips go too
])
def test_more_than_half_is_the_line_across_the_dataset(monkeypatch, tmp_path, gone_in_campus, trips):
    rows = [row(n) for n in range(60)] + [row(900 + n, OTHER) for n in range(40)]
    search = {CAMPUS: [listed(n) for n in range(gone_in_campus, 60)],
              OTHER: [listed(900 + n, OTHER) for n in range(20, 40)]}        # 20 of OTHER's 40 gone
    out = night(monkeypatch, tmp_path, rows, search)

    other_flipped = sum(status_of(out, 900 + n) == ["Expired"] for n in range(40))
    assert other_flipped == (0 if trips else 20)
    assert stamped(tmp_path) is (not trips)


def test_a_dropped_entity_is_scanned_again_by_a_same_day_rerun_not_skipped_as_done(monkeypatch, tmp_path):
    """Left in the day's progress file it would be skipped, and the rerun could finish the night and
    stamp it complete over flips that were never made."""
    rows = [row(n) for n in range(60)] + [row(900 + n, OTHER) for n in range(3)]
    night(monkeypatch, tmp_path, rows, {})                                    # the outage

    search = {CAMPUS: [listed(n) for n in range(60)], OTHER: [listed(900, OTHER), listed(901, OTHER)]}
    out = night(monkeypatch, tmp_path, None, search)                          # the state is back

    assert status_of(out, 902) == ["Expired"], "OTHER was read again and its ending applied"
    assert status_of(out, 900) == ["Active"] and status_of(out, 901) == ["Active"]
    assert stamped(tmp_path)
    [entry] = report(tmp_path)
    assert entry["complete"] is True and entry["entities"][OTHER]["flipped_to_expired"] == 1


def test_the_breaker_does_not_exit_and_the_night_stays_open_for_the_freshness_check(monkeypatch, tmp_path):
    """Like a refused entity, it holds the night open rather than failing the step; the last
    step of daily.yml (check_scrape_stamps.py) is what turns the run red."""
    rows = [row(n) for n in range(60)] + [row(900 + n, OTHER) for n in range(3)]
    night(monkeypatch, tmp_path, rows, {})            # returns normally: no SystemExit
    assert not stamped(tmp_path)


@pytest.mark.parametrize("known, vanished, trips", [
    (49, 49, False), (50, 50, True), (50, 25, False), (50, 26, True),
    (1000, 500, False), (1000, 501, True), (0, 0, False), (100, 0, False),
])
def test_dataset_breaker_boundaries(known, vanished, trips):
    assert (scrape.dataset_breaker(known, vanished) is not None) is trips


# --- the refusal's boundaries -------------------------------------------------
#
# Pinned from both sides because each is one character in scrape.refuse_flips:
# ">= 20" against "> 20", and "> 0.5" against ">= 0.5". `seen` is what the
# search still lists, so what vanished is known minus seen.

@pytest.mark.parametrize("known,seen,refused", [
    (20, 9, True),     # 11 of 20 gone: over half
    (20, 10, False),   # exactly half is not "more than half"
    (21, 10, True),    # 11 of 21 gone
    (21, 11, False),   # 10 of 21 gone
    (19, 8, False),    # under 20 known, the proportional rule does not apply...
    (19, 3, False),    # ...however much has gone: the band the guard rail alone watches
    (19, 0, True),     # ...except all of it, which the floor catches
])
def test_the_proportional_refusal_boundaries(known, seen, refused):
    assert bool(scrape.refuse_flips(urls(*range(known)), urls(*range(seen)))) is refused


# --- the daily progress file ----------------------------------------------------
#
# It exists so a same-day retry skips entities already done tonight. A file from
# an earlier day must read as empty, or every entity "done" yesterday is skipped
# tonight and its flips are never computed -- the night finishes green without
# having looked at it. Nothing pinned the date check: inverting it, or dropping
# it, left every test passing.

def _progress(path, day, entities):
    path.write_text(json.dumps({"date": day.isoformat(), "done_entities": sorted(entities)}),
                    encoding="utf-8")


def test_progress_saved_today_loads_back_unchanged(tmp_path):
    path = str(tmp_path / "progress.json")
    scrape.save_daily_progress(path, {CAMPUS, OTHER})
    assert scrape.load_daily_progress(path) == {CAMPUS, OTHER}


def test_progress_from_yesterday_is_ignored(tmp_path):
    import datetime

    path = tmp_path / "progress.json"
    _progress(path, datetime.date.today() - datetime.timedelta(days=1), {CAMPUS})
    assert scrape.load_daily_progress(str(path)) == set()


def test_progress_dated_today_is_resumed(tmp_path):
    import datetime

    path = tmp_path / "progress.json"
    _progress(path, datetime.date.today(), {CAMPUS})
    assert scrape.load_daily_progress(str(path)) == {CAMPUS}


@pytest.mark.parametrize("content", ['{"date": "2026-', "", "not json at all"])
def test_a_truncated_progress_file_is_a_fresh_start(tmp_path, content):
    path = tmp_path / "progress.json"
    path.write_text(content, encoding="utf-8")
    assert scrape.load_daily_progress(str(path)) == set()


def test_a_missing_progress_file_is_a_fresh_start(tmp_path):
    assert scrape.load_daily_progress(str(tmp_path / "nope.json")) == set()


def test_yesterdays_progress_does_not_suppress_tonights_scan(monkeypatch, tmp_path):
    """The consequence, end to end: both entities were 'done' yesterday. Tonight
    CHADRON lists 9 of its 10 records, so one must flip -- which can only happen
    if the stale file did not make the run skip it."""
    import datetime

    (tmp_path / "data").mkdir()
    _progress(tmp_path / "data" / "contract_daily_progress.json",
              datetime.date.today() - datetime.timedelta(days=1), {CAMPUS, OTHER})
    rows = [row(n) for n in range(10)] + [row(99, OTHER)]
    out = night(monkeypatch, tmp_path, rows,
                {CAMPUS: [listed(n) for n in range(9)], OTHER: [listed(99, OTHER)]})

    assert status_of(out, 9) == ["Expired"]
    assert stamped(tmp_path)


def test_todays_progress_makes_a_retry_skip_the_entities_already_done(monkeypatch, tmp_path):
    """And the other half: a same-day retry must not re-scan what already
    finished. CHADRON is marked done tonight, so its missing record is not
    flipped by this run even though the search no longer lists it."""
    import datetime

    (tmp_path / "data").mkdir()
    _progress(tmp_path / "data" / "contract_daily_progress.json", datetime.date.today(), {CAMPUS})
    rows = [row(n) for n in range(10)] + [row(99, OTHER)]
    out = night(monkeypatch, tmp_path, rows,
                {CAMPUS: [listed(n) for n in range(9)], OTHER: [listed(99, OTHER)]})

    assert status_of(out, 9) == ["Active"]


# --- the scrape time ----------------------------------------------------------------

def test_recording_one_datasets_time_keeps_the_others(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "scrape_meta.json").write_text(
        json.dumps({"purchase-order": "2026-10-12T03:08:00+00:00", "state": "2026-09-24T03:07:01+00:00"}),
        encoding="utf-8")

    stamp = scrape.record_scrape_time("contract")

    stamps = json.loads((tmp_path / "data" / "scrape_meta.json").read_text(encoding="utf-8"))
    assert stamps["contract"] == stamp
    assert stamps["purchase-order"] == "2026-10-12T03:08:00+00:00"
    assert stamps["state"] == "2026-09-24T03:07:01+00:00", "a stale dataset's stamp must survive another's"


def test_recording_a_time_survives_an_unreadable_meta_file(monkeypatch, tmp_path):
    """Start fresh rather than fail a scrape that has just succeeded."""
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "scrape_meta.json").write_text('{"contract": "2026-', encoding="utf-8")

    stamp = scrape.record_scrape_time("state")

    assert json.loads((tmp_path / "data" / "scrape_meta.json").read_text(encoding="utf-8")) == {"state": stamp}


# --- merging a night's report ----------------------------------------------------------

def _counts(still, flipped, previous=10):
    return {"previously_active": previous, "still_active": still, "newly_active": 0,
            "flipped_to_expired": flipped}


def _report_at(monkeypatch, tmp_path):
    path = tmp_path / "daily_diff_report.json"
    monkeypatch.setattr(scrape, "DAILY_DIFF_REPORT", str(path))
    return path


def test_a_rerun_the_same_night_keeps_an_entitys_first_counts(monkeypatch, tmp_path):
    """Those carry the real flips: a re-scan after the patch would report none."""
    path = _report_at(monkeypatch, tmp_path)
    scrape.append_daily_diff_report("contract", {CAMPUS: _counts(4, 6)}, "2026-10-01T23:00:00-05:00", complete=False)
    scrape.append_daily_diff_report("contract", {CAMPUS: _counts(10, 0)}, "2026-10-01T23:30:00-05:00", complete=False)

    [entry] = json.loads(path.read_text(encoding="utf-8"))
    assert entry["entities"][CAMPUS] == _counts(4, 6)
    assert entry["timestamp"] == "2026-10-01T23:30:00-05:00"
    assert entry["totals"]["flipped_to_expired"] == 6


def test_a_rerun_adds_the_entities_it_newly_finished(monkeypatch, tmp_path):
    path = _report_at(monkeypatch, tmp_path)
    scrape.append_daily_diff_report("contract", {CAMPUS: _counts(4, 6)}, "2026-10-01T23:00:00-05:00", complete=False)
    scrape.append_daily_diff_report("contract", {OTHER: _counts(10, 0)}, "2026-10-01T23:30:00-05:00", complete=True)

    [entry] = json.loads(path.read_text(encoding="utf-8"))
    assert set(entry["entities"]) == {CAMPUS, OTHER}
    assert entry["complete"] is True
    assert entry["totals"]["previously_active"] == 20


def test_two_nights_are_never_merged(monkeypatch, tmp_path):
    path = _report_at(monkeypatch, tmp_path)
    scrape.append_daily_diff_report("contract", {CAMPUS: _counts(4, 6)}, "2026-10-01T23:00:00-05:00", complete=False)
    scrape.append_daily_diff_report("contract", {CAMPUS: _counts(4, 6)}, "2026-10-02T23:00:00-05:00", complete=False)

    assert len(json.loads(path.read_text(encoding="utf-8"))) == 2


def test_two_datasets_are_never_merged(monkeypatch, tmp_path):
    path = _report_at(monkeypatch, tmp_path)
    scrape.append_daily_diff_report("contract", {CAMPUS: _counts(4, 6)}, "2026-10-01T23:00:00-05:00", complete=False)
    scrape.append_daily_diff_report("state", {CAMPUS: _counts(4, 6)}, "2026-10-01T23:10:00-05:00", complete=False)

    entries = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(e["dataset"] for e in entries) == ["contract", "state"]


def test_a_finished_night_is_never_merged_into_a_later_unfinished_one(monkeypatch, tmp_path):
    path = _report_at(monkeypatch, tmp_path)
    scrape.append_daily_diff_report("contract", {CAMPUS: _counts(10, 0)}, "2026-10-01T23:00:00-05:00", complete=True)
    scrape.append_daily_diff_report("contract", {OTHER: _counts(4, 6)}, "2026-10-01T23:40:00-05:00", complete=False)

    assert len(json.loads(path.read_text(encoding="utf-8"))) == 2


# --- flipping back a record that is listed again ---------------------------------
#
# Until Oct 2026 --daily only ever flipped Active -> Expired. A record listed again
# was unknown, so it was fetched and appended as a new Active row beside its stale
# Expired one, and the CSV held both. Now --daily notes its own flips and flips a
# returning record's row back. The danger is the other way round: an Active search
# whose Status filter leaks Expired records would flip back records that really
# ended, so the leak is judged on records --daily never flipped.

def ledger(tmp_path):
    path = tmp_path / "data" / "contract_daily_flips.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def keys(*names):
    """The ledger's keys for these records: hashed, like load_expired_only's."""
    return {scrape.ledger_key(u) for u in urls(*names)}


def urls_with_both(rows):
    statuses = {}
    for r in rows:
        statuses.setdefault(r[9], set()).add(r[8])
    return [u for u, s in statuses.items() if s == {"Active", "Expired"}]


def test_a_record_missed_one_night_is_flipped_back_not_appended(monkeypatch, tmp_path):
    rows = [row(n) for n in range(10)] + [row(99, OTHER)]
    out = night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(9)],
                                              OTHER: [listed(99, OTHER)]})
    assert status_of(out, 9) == ["Expired"]
    assert set(ledger(tmp_path)) == keys(9)

    out = night(monkeypatch, tmp_path, None, {CAMPUS: [listed(n) for n in range(10)],
                                              OTHER: [listed(99, OTHER)]})

    assert status_of(out, 9) == ["Active"], "flipped back, and the only row it has"
    assert len(out) == 11, "nothing appended"
    assert out[9][10] == RESTORED_VIEW, "the same row, restored with tonight's detail fetch"
    assert ledger(tmp_path) == {}
    entry = report(tmp_path)[-1]
    assert entry["entities"][CAMPUS]["flipped_back"] == 1
    assert entry["entities"][CAMPUS]["newly_active"] == 0
    assert entry["totals"]["flipped_back"] == 1


def test_an_outage_spread_over_two_nights_is_undone_in_one(monkeypatch, tmp_path):
    """Night one the small body answers empty and its four flip; night two the large one
    drops ten, under every refusal. All fourteen come back on night three -- a share no
    refusal of "too many came back" would let through, which is why none exists."""
    rows = [row(n) for n in range(60)] + [row(900 + n, OTHER) for n in range(4)]
    everything = {CAMPUS: [listed(n) for n in range(60)],
                  OTHER: [listed(900 + n, OTHER) for n in range(4)]}
    night(monkeypatch, tmp_path, rows, {CAMPUS: everything[CAMPUS]})
    night(monkeypatch, tmp_path, None, {CAMPUS: everything[CAMPUS][10:], OTHER: everything[OTHER]})
    out = night(monkeypatch, tmp_path, None, everything)

    assert all(r[8] == "Active" for r in out)
    assert len(out) == 64 and urls_with_both(out) == []
    assert report(tmp_path)[-1]["totals"]["flipped_back"] == 10
    assert ledger(tmp_path) == {}
    assert stamped(tmp_path)


def full_scrape_expired(n, entity=CAMPUS):
    """A row the full scrape read from the Expired search -- never flipped by --daily."""
    return row(n, entity, status="Expired")


def test_a_leaking_status_filter_flips_nothing_back_and_appends_nothing(monkeypatch, tmp_path, capsys):
    rows = ([row(n) for n in range(20)] + [full_scrape_expired(500 + n) for n in range(40)]
            + [row(99, OTHER)])
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(19)],
                                        OTHER: [listed(99, OTHER)]})
    assert set(ledger(tmp_path)) == keys(19)
    capsys.readouterr()

    leaking = [listed(n) for n in range(20)] + [listed(500 + n) for n in range(40)]
    out = night(monkeypatch, tmp_path, None, {CAMPUS: leaking, OTHER: [listed(99, OTHER)]})

    assert status_of(out, 19) == ["Expired"], "its own flip is not trusted back on a leaky night"
    assert all(status_of(out, 500 + n) == ["Expired"] for n in range(40))
    assert len(out) == 61, "no leaked record appended as a new Active row"
    printed = capsys.readouterr().out
    assert "::warning title=Daily scrape: the Active search is listing Expired records::" in printed
    assert "listed 40 record(s) the state had already filed as Expired, of 60 listed" in printed
    assert stamped(tmp_path), "a leak adds to what was seen; the night's endings stand and it finishes"
    assert report(tmp_path)[-1]["entities"][CAMPUS]["flipped_back"] == 0
    assert report(tmp_path)[-1]["entities"][CAMPUS]["newly_active"] == 0, "the leaked records were not appended"
    assert set(ledger(tmp_path)) == keys(19), "still owed a flip back"

    out = night(monkeypatch, tmp_path, None, {CAMPUS: [listed(n) for n in range(20)],
                                              OTHER: [listed(99, OTHER)]})
    assert status_of(out, 19) == ["Active"], "flipped back once the filter holds again"


def test_a_contract_genuinely_reopened_is_flipped_back(monkeypatch, tmp_path):
    """A few records the full scrape filed as Expired, listed Active again, are below the line."""
    rows = [row(n) for n in range(100)] + [full_scrape_expired(500 + n) for n in range(40)]
    out = night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(100)]
                                              + [listed(500 + n) for n in range(4)]}, entities=(CAMPUS,))

    assert [status_of(out, 500 + n) for n in range(5)] == [["Active"]] * 4 + [["Expired"]]
    assert len(out) == 140 and urls_with_both(out) == []


def test_strays_over_the_floor_but_under_the_share_are_flipped_back(monkeypatch, tmp_path):
    rows = [row(n) for n in range(200)] + [full_scrape_expired(500 + n) for n in range(40)]
    out = night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(200)]
                                              + [listed(500 + n) for n in range(10)]}, entities=(CAMPUS,))
    assert sum(status_of(out, 500 + n) == ["Active"] for n in range(40)) == 10   # 10 of 210 is under 5%


def test_a_leak_spread_thin_across_entities_is_caught_across_the_dataset(monkeypatch, tmp_path, capsys):
    """Four strays in each of two small entities: each under the floor, eight together are not."""
    rows = ([row(n) for n in range(3)] + [full_scrape_expired(500 + n) for n in range(10)]
            + [row(900 + n, OTHER) for n in range(3)] + [full_scrape_expired(600 + n, OTHER) for n in range(10)])
    search = {CAMPUS: [listed(n) for n in range(3)] + [listed(500 + n) for n in range(4)],
              OTHER: [listed(900 + n, OTHER) for n in range(3)] + [listed(600 + n, OTHER) for n in range(4)]}
    out = night(monkeypatch, tmp_path, rows, search)

    assert all(r[8] == "Expired" for r in out if 500 <= int(r[0]) < 700)
    assert len(out) == 26
    assert "across the dataset, the Active search listed 8 record(s)" in capsys.readouterr().out
    assert report(tmp_path)[-1]["totals"]["flipped_back"] == 0
    assert stamped(tmp_path)


def test_its_own_flips_coming_back_never_count_as_a_leak(monkeypatch, tmp_path):
    """Fifty of --daily's own flips listed again, with nothing else: all fifty flip back."""
    rows = [row(n) for n in range(60)]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(60)]}, entities=(CAMPUS,))
    ledger_path = tmp_path / "data" / "contract_daily_flips.json"
    scrape.patch_status_to_expired(str(tmp_path / "data" / "nu_contracts.csv"), urls(*range(10, 60)))
    # Raw Detail URLs, as the ledger was written before it was hashed: they still count as owed.
    import datetime
    ledger_path.write_text(json.dumps({u: datetime.date.today().isoformat() for u in urls(*range(10, 60))}),
                           encoding="utf-8")

    out = night(monkeypatch, tmp_path, None, {CAMPUS: [listed(n) for n in range(60)]}, entities=(CAMPUS,))
    assert all(r[8] == "Active" for r in out) and len(out) == 60


def test_only_the_row_it_flipped_comes_back_beside_an_older_twin(monkeypatch, tmp_path):
    """A URL left with a stale Expired row and an appended Active one before Oct 2026.
    When it is missed and then listed again, only its last row -- the one --daily flipped --
    returns to Active, so it is not listed as Active twice."""
    stale = row(9, status="Expired")
    rows = [row(n) for n in range(10)] + [stale] + [row(9)] + [row(99, OTHER)]
    rows.pop(9)     # the stale twin sits before the Active row appended after it
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(9)], OTHER: [listed(99, OTHER)]})
    out = night(monkeypatch, tmp_path, None, {CAMPUS: [listed(n) for n in range(10)],
                                              OTHER: [listed(99, OTHER)]})
    assert status_of(out, 9) == ["Expired", "Active"]


def test_an_amendment_made_while_off_the_list_is_recorded(monkeypatch, tmp_path):
    rows = [row(n) for n in range(10)]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(9)]}, entities=(CAMPUS,))
    night(monkeypatch, tmp_path, None, {CAMPUS: [listed(n) for n in range(9)] + [listed(9, amount="$250.00")]},
          entities=(CAMPUS,))

    [change] = [json.loads(l) for l in (tmp_path / "data" / "changes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert (change["doc"], change["field"], change["from"], change["to"]) == ("9", "amount", "$100.00", "$250.00")


def test_an_outage_night_flips_nothing_back_either(monkeypatch, tmp_path):
    """The breaker drops the whole night, flip-backs included, and the ledger keeps them owed."""
    rows = [row(n) for n in range(60)] + [row(900 + n, OTHER) for n in range(3)]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(60)],
                                        OTHER: [listed(900, OTHER), listed(901, OTHER)]})
    assert set(ledger(tmp_path)) == keys(902)
    out = night(monkeypatch, tmp_path, None, {OTHER: [listed(900 + n, OTHER) for n in range(3)]})
    assert status_of(out, 902) == ["Expired"] and set(ledger(tmp_path)) == keys(902)


@pytest.mark.parametrize("strays, listed_count, leak", [
    (4, 4, False),        # under the floor, however large a share
    (5, 100, False),      # exactly 5% is not more than 5%
    (5, 99, True),
    (50, 1000, False),
    (51, 1000, True),
    (0, 0, False),
])
def test_leak_boundaries(strays, listed_count, leak):
    assert (scrape.leak_suspected(strays, listed_count) is not None) is leak


def test_an_unreadable_ledger_is_set_aside_and_started_fresh(tmp_path, capsys):
    path = tmp_path / "flips.json"
    path.write_text('{"https://exa', encoding="utf-8")
    assert scrape.load_flip_ledger(str(path)) == {}
    assert "::warning title=Unreadable state file::" in capsys.readouterr().out
    assert (tmp_path / "flips.json.corrupt").exists()
