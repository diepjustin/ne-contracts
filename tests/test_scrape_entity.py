"""scrape_entity and run_daily as they actually run together.

test_status_flips.py replaces scrape_entity with a fake that implements its
contract itself -- including calling on_record_seen for every record -- so the
contract the nightly depends on was only ever tested against its own
description. These tests drive the real function with a scripted search, so the
parts the fake assumes are checked: every parsed record reaches the caller even
when the filter rejects it, an empty page mid-run is re-searched exactly once
and then given up on without claiming completion, a deadline stops the combo
unfinished, and the checkpoint files round-trip.

Nothing here touches the network or data/: the search, the token and the
document fetches are scripted, and documents.jsonl is not written.
"""

import csv
import json
import os
import sys
import time
import types

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import check_entity_drift  # noqa: E402
import scrape  # noqa: E402

ENTITY = "Chadron State College"
OTHER = "Peru State College"


class Response:
    text = "<html></html>"

    def raise_for_status(self):
        pass


class Session:
    """Counts what scrape_entity asks of the server: searches (POST) and pages (GET)."""

    def __init__(self):
        self.posts = 0
        self.gets = []

    def post(self, *args, **kwargs):
        self.posts += 1
        return Response()

    def get(self, url, *args, **kwargs):
        self.gets.append(url)
        return Response()


class Recorder:
    def __init__(self):
        self.rows = []

    def writerow(self, row):
        self.rows.append(row)


def rec(i, entity=ENTITY):
    return {"doc_number": f"D{i}", "doc_type": "CN", "entity_code": "051", "entity_name": entity.upper(),
            "vendor": f"VENDOR {i}", "amount": "$1.00", "begin_date": "01/01/2026",
            "end_date": "12/31/2026",
            "detail_url": f"https://example.test/Search/SearchDocuments?DN=n{i}&V=v{i}"}


def csv_row(i, status="Active", entity=ENTITY):
    return [f"D{i}", "CN", "051", entity, f"VENDOR {i}", "$1.00", "01/01/2026", "12/31/2026", status,
            f"https://example.test/Search/SearchDocuments?DN=n{i}&V=v{i}", ""]


@pytest.fixture
def search(monkeypatch):
    """Script the server: `pages` are what parse_results_page returns, one entry per
    response scrape_entity reads, as (records, last_page)."""
    fetched = []

    def script(pages):
        it = iter(pages)
        monkeypatch.setattr(scrape, "parse_results_page", lambda _soup: next(it))
        return Session()

    monkeypatch.setattr(scrape, "get_token", lambda _s: "token")
    monkeypatch.setattr(scrape.time, "sleep", lambda _s: None)
    monkeypatch.setattr(scrape, "append_documents", lambda entries, path=None: len(entries))

    def documents(records):
        fetched.append([r["doc_number"] for r in records])
        return [[{"name": "DOC", "size": "1Mb", "token": "T" + r["doc_number"]}] for r in records]

    monkeypatch.setattr(scrape, "fetch_documents_parallel", documents)
    script.fetched = fetched
    return script


def run_entity(session, writer, **kwargs):
    return scrape.scrape_entity(session, ENTITY, "1", "Active", "E", "CN", writer, **kwargs)


# --- scrape_entity ------------------------------------------------------------

def test_every_parsed_record_reaches_the_caller_even_when_the_filter_rejects_it(search):
    """The contract run_daily's diff rests on. `seen` is built from on_record_seen,
    and known records are rejected by record_filter so they cost no fetch -- if
    they stopped being reported, every known record would look vanished."""
    session = search([([rec(1), rec(2), rec(3)], 1)])
    seen, writer = [], Recorder()
    total, finished = run_entity(session, writer,
                                 record_filter=lambda r: r["doc_number"] == "D3",
                                 on_record_seen=lambda r: seen.append(r["doc_number"]))

    assert (total, finished) == (3, True)
    assert seen == ["D1", "D2", "D3"]
    assert [r[0] for r in writer.rows] == ["D3"], "only the record that passed the filter is written"
    assert search.fetched == [["D3"]], "and only that one is fetched"


def test_an_empty_first_page_is_a_clean_finish_with_nothing(search, capsys):
    """The premise of the 17 Aug 2026 incident and of refuse_flips: an entity the
    search answers 'No results found' for finishes cleanly with zero records, so
    the caller has to judge whether zero is believable."""
    session = search([([], None)])
    assert run_entity(session, Recorder()) == (0, True)
    assert "No results found" in capsys.readouterr().out


def test_a_blank_page_mid_run_is_re_searched_once_and_the_run_resumes(search):
    """The site holds results in session state that expires; page 2 comes back
    blank, with no pager. Re-running the search and returning to page 2 is the
    only way on."""
    session = search([([rec(1), rec(2)], 2), ([], None), ([rec(3), rec(4)], 2)])
    writer = Recorder()
    total, finished = run_entity(session, writer)

    assert (total, finished) == (4, True)
    assert [r[0] for r in writer.rows] == ["D1", "D2", "D3", "D4"]
    assert session.posts == 2, "the first search plus exactly one re-search"


def test_a_page_still_blank_after_the_re_search_stops_without_claiming_completion(search, capsys):
    """A blank page has no pager, so last_page is None and nothing after the loop
    can notice the combo stopped short: this return is all that stops a 7,139-
    page combo from quietly finishing at 5,777 and marking itself complete."""
    session = search([([rec(1), rec(2)], 3), ([], None), ([], None)])
    writer = Recorder()
    total, finished = run_entity(session, writer)

    assert (total, finished) == (2, False)
    assert session.posts == 2, "one re-search, not a loop"
    assert "still empty after re-running the search" in capsys.readouterr().out


def test_a_deadline_stops_the_combo_unfinished_after_the_page_in_hand(search):
    session = search([([rec(1), rec(2)], 3), ([rec(3)], 3)])
    writer = Recorder()
    total, finished = run_entity(session, writer, deadline=time.time() - 1)

    assert (total, finished) == (2, False)
    assert [r[0] for r in writer.rows] == ["D1", "D2"], "the page already read is kept"


def test_a_deadline_does_not_cut_a_combo_that_is_already_on_its_last_page(search):
    session = search([([rec(1)], 1)])
    assert run_entity(session, Recorder(), deadline=time.time() - 1) == (1, True)


def test_a_resumed_combo_asks_for_its_own_page_first(search):
    session = search([([rec(7)], 3)])
    total, finished = run_entity(session, Recorder(), start_page=3)

    assert (total, finished) == (1, True)
    assert session.gets and session.gets[0].endswith("page=3")


def test_unreadable_detail_pages_leave_no_row_and_say_so(search, monkeypatch, capsys):
    monkeypatch.setattr(scrape, "fetch_documents_parallel", lambda records: [None for _ in records])
    writer = Recorder()
    total, finished = run_entity(search([([rec(1), rec(2)], 1)]), writer)

    assert (total, finished) == (2, True)
    assert writer.rows == []
    assert "2 record(s) held back" in capsys.readouterr().out


# --- run_daily through the real scrape_entity ----------------------------------------

@pytest.fixture
def daily(monkeypatch, tmp_path, search):
    """One `scrape.py contract --daily` for ENTITY, over a CSV already holding `known`."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(scrape, "document_service_healthy", lambda: True)
    # The dataset is ENTITY alone, so a night that reads it is a complete one.
    monkeypatch.setattr(scrape, "HIGHER_ED_ENTITIES", {ENTITY: "1"})

    def run(known, pages, statuses=None, tail=""):
        csv_path = tmp_path / "data" / "nu_contracts.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(scrape.CSV_HEADER)
            for i in known:
                writer.writerow(csv_row(i, (statuses or {}).get(i, "Active")))
            f.write(tail)       # what a killed write leaves: text with no newline
        session = search(pages)
        monkeypatch.setattr(scrape, "build_session", lambda: session)
        scrape.run_daily(types.SimpleNamespace(dataset="contract", entity=[ENTITY], hours=None))
        with open(csv_path, newline="", encoding="utf-8") as f:
            return [r for r in csv.reader(f)][1:]

    run.tmp = tmp_path
    return run


def status_of(rows, i):
    return [r[8] for r in rows if r[0] == f"D{i}"]


def stamp_written(tmp_path):
    path = tmp_path / "data" / "scrape_meta.json"
    return path.exists() and "contract" in json.loads(path.read_text(encoding="utf-8"))


def test_known_records_that_are_all_still_listed_flip_nothing_and_cost_no_fetch(daily, search):
    """30 known Active records, all 30 listed across two pages. If the known ones
    were not reported as seen, all 30 would look vanished and this night would
    be refused as an empty answer."""
    rows = daily(range(30), [([rec(i) for i in range(25)], 2), ([rec(i) for i in range(25, 30)], 2)])

    assert [status_of(rows, i) for i in range(30)] == [["Active"]] * 30
    assert len(rows) == 30, "nothing appended"
    assert search.fetched == [[], []], "known records are never fetched"
    assert stamp_written(daily.tmp), "a night that read everything finishes and stamps"
    [entry] = json.loads((daily.tmp / "data" / "daily_diff_report.json").read_text(encoding="utf-8"))
    assert entry["entities"][ENTITY] == {"previously_active": 30, "still_active": 30,
                                         "newly_active": 0, "held_back": 0,
                                         "flipped_to_expired": 0, "flipped_back": 0}


def test_a_new_record_is_appended_and_a_vanished_one_flips(daily, search):
    rows = daily(range(10), [([rec(i) for i in range(1, 11)], 1)])

    assert status_of(rows, 0) == ["Expired"], "listed last night, not tonight"
    assert [status_of(rows, i) for i in range(1, 10)] == [["Active"]] * 9
    assert status_of(rows, 10) == ["Active"], "new tonight, appended"
    assert search.fetched == [["D10"]], "details are fetched for the new record only"
    assert stamp_written(daily.tmp)


def test_an_amendment_to_a_known_record_is_recorded_without_a_fetch(daily):
    changed = dict(rec(3), amount="$9.99")
    daily(range(5), [([rec(0), rec(1), rec(2), changed, rec(4)], 1)])

    entries = [json.loads(l) for l in (daily.tmp / "data" / "changes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(e["doc"], e["field"], e["from"], e["to"]) for e in entries] == [("D3", "amount", "$1.00", "$9.99")]


def test_an_empty_answer_for_five_known_records_is_refused_end_to_end(daily):
    rows = daily(range(5), [([], None)])

    assert [status_of(rows, i) for i in range(5)] == [["Active"]] * 5
    assert not stamp_written(daily.tmp), "a refused entity holds the night open"


def test_a_record_listed_again_is_restored_through_the_real_scrape_entity(daily, search):
    """The flip-back path end to end: D9 is Expired and owed (in the ledger). Tonight it is
    listed with a new amount. It costs no fetch during the scrape -- the CSV holds it -- and
    one detail fetch once the night is judged, and its row comes back with tonight's values."""
    url9 = rec(9)["detail_url"]
    (daily.tmp / "data" / "contract_daily_flips.json").write_text(
        json.dumps({scrape.ledger_key(url9): "2026-10-22"}), encoding="utf-8")
    rows = daily(range(10), [([rec(i) for i in range(9)] + [dict(rec(9), amount="$7.00")], 1)],
                 statuses={9: "Expired"})

    assert len(rows) == 10, "restored in place"
    [r9] = [r for r in rows if r[0] == "D9"]
    assert (r9[5], r9[8], r9[10]) == ("$7.00", "Active", scrape.view_url_for([{"token": "TD9"}]))
    assert search.fetched == [[], ["D9"]], "no fetch while scraping, one for the restore"
    assert json.loads((daily.tmp / "data" / "contract_daily_flips.json").read_text(encoding="utf-8")) == {}


def test_a_leaky_night_writes_neither_rows_nor_documents_for_records_it_never_saw(daily, search, monkeypatch):
    written = []
    monkeypatch.setattr(scrape, "append_documents", lambda entries, path=None: written.extend(entries) or len(entries))
    statuses = {i: "Expired" for i in range(20, 60)}
    rows = daily(range(60), [([rec(i) for i in range(65)], 1)], statuses=statuses)

    assert len(rows) == 60, "the five never-seen records are held back"
    assert written == [], "and so are their documents entries, so the two files still agree"
    [entry] = json.loads((daily.tmp / "data" / "daily_diff_report.json").read_text(encoding="utf-8"))
    assert entry["entities"][ENTITY]["held_back"] == 5


# --- the checkpoints ---------------------------------------------------------------

def test_drop_unfinished_rows_keeps_only_combos_with_a_position(tmp_path):
    """A combo cut off before its first page was checkpointed would be re-scraped
    and duplicated, so its rows go; a finished or partly-positioned combo keeps
    them. A row too short to carry a Status -- the tail of a killed write -- goes
    with them rather than raising."""
    path = tmp_path / "rows.csv"
    rows = [csv_row(1, "Active", "A"), csv_row(2, "Expired", "A"), csv_row(3, "Active", "B"),
            ["D4", "CN", "051", "B"]]
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([scrape.CSV_HEADER] + rows)

    dropped = scrape.drop_unfinished_rows(str(path), {("A", "Active"), ("B", "Expired")})

    with open(path, newline="", encoding="utf-8") as f:
        kept = list(csv.reader(f))
    assert dropped == 3
    assert [r[0] for r in kept] == ["Document Number", "D1"]


def test_drop_unfinished_rows_leaves_an_untouched_file_alone(tmp_path):
    path = tmp_path / "rows.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        # LF, not the csv module's CRLF, so a needless rewrite changes the bytes
        csv.writer(f, lineterminator="\n").writerows([scrape.CSV_HEADER, csv_row(1, "Active", "A")])
    before = path.read_bytes()

    assert scrape.drop_unfinished_rows(str(path), {("A", "Active")}) == 0
    assert path.read_bytes() == before
    assert scrape.drop_unfinished_rows(str(tmp_path / "missing.csv"), set()) == 0


def write_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f, lineterminator="\n").writerows([scrape.CSV_HEADER] + rows)
    return path


def test_more_unaccounted_rows_than_a_cut_off_scrape_can_leave_are_refused_and_nothing_changes(tmp_path):
    """The progress file is what vouches for the rows. If it says nothing about 1,001 of them,
    the file is wrong, not the CSV: dropping them would delete the dataset."""
    path = write_rows(tmp_path / "rows.csv", [csv_row(i, "Active", "A") for i in range(1001)])
    before = path.read_bytes()
    with pytest.raises(scrape.UnaccountedRows) as stopped:
        scrape.drop_unfinished_rows(str(path), set(), limit=1000)
    assert stopped.value.count == 1001
    assert path.read_bytes() == before and not (tmp_path / "rows.csv.tmp").exists()


def test_exactly_the_limit_is_still_a_cut_off_scrape(tmp_path):
    path = write_rows(tmp_path / "rows.csv", [csv_row(i, "Active", "A") for i in range(1000)])
    assert scrape.drop_unfinished_rows(str(path), set(), limit=1000) == 1000


def test_the_limit_is_what_main_passes_and_it_is_about_a_page():
    assert scrape.MAX_UNACCOUNTED_ROWS == 1000


def test_a_kill_during_the_rewrite_leaves_the_original_csv_whole(tmp_path, monkeypatch):
    """It used to open the CSV for writing in place, so an interrupted rewrite left a
    truncated file that every later check would accept."""
    path = write_rows(tmp_path / "rows.csv", [csv_row(i, "Active", "A") for i in range(4)]
                      + [csv_row(9, "Active", "B")])
    before = path.read_bytes()
    real_writer = csv.writer

    class Dies:
        def __init__(self, f):
            self.inner = real_writer(f)

        def writerow(self, row):
            self.inner.writerow(row)

        def writerows(self, rows):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(scrape.csv, "writer", lambda f: Dies(f))
    with pytest.raises(OSError):
        scrape.drop_unfinished_rows(str(path), {("A", "Active")})
    assert path.read_bytes() == before
    assert not (tmp_path / "rows.csv.tmp").exists(), "the half-written copy is removed"


def test_progress_round_trips_with_page_positions(tmp_path):
    path = str(tmp_path / "progress.json")
    scrape.save_progress(path, {("A", "Active"), ("B", "Expired")}, {("C", "Active"): 17})

    assert scrape.load_progress(path) == ({("A", "Active"), ("B", "Expired")}, {("C", "Active"): 17})
    assert not os.path.exists(path + ".tmp"), "written atomically, nothing left beside it"


def test_progress_in_the_legacy_list_format_still_loads(tmp_path):
    path = tmp_path / "progress.json"
    path.write_text(json.dumps([["A", "Active"], ["B", "Expired"]]), encoding="utf-8")
    assert scrape.load_progress(str(path)) == ({("A", "Active"), ("B", "Expired")}, {})


@pytest.mark.parametrize("content", [None, "", '{"done": [["A", "Act'])
def test_missing_or_corrupt_progress_is_a_fresh_start(tmp_path, content):
    path = tmp_path / "progress.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert scrape.load_progress(str(path)) == (set(), {})


# --- patching a night's flips into the CSV --------------------------------------------

def test_patch_flips_only_active_rows_whose_url_is_listed(tmp_path):
    path = tmp_path / "rows.csv"
    rows = [csv_row(1), csv_row(2), csv_row(3, "Expired")]
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([scrape.CSV_HEADER] + rows)

    patched = scrape.patch_status_to_expired(str(path), {rows[0][9], rows[2][9]})

    with open(path, newline="", encoding="utf-8") as f:
        out = list(csv.reader(f))[1:]
    assert patched == 1, "the Expired row in the set is not counted again"
    assert [r[8] for r in out] == ["Expired", "Active", "Expired"]
    assert not os.path.exists(str(path) + ".tmp")


def test_patch_with_nothing_to_flip_does_not_rewrite_the_file(tmp_path):
    path = tmp_path / "rows.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        # LF, not the csv module's CRLF, so a needless rewrite changes the bytes
        csv.writer(f, lineterminator="\n").writerows([scrape.CSV_HEADER, csv_row(1)])
    before = path.read_bytes()

    assert scrape.patch_status_to_expired(str(path), {"https://example.test/not-there"}) == 0
    assert scrape.patch_status_to_expired(str(path), set()) == 0
    assert path.read_bytes() == before


# --- the entity-list drift check --------------------------------------------------------

def test_entity_drift_reports_added_removed_and_changed(capsys):
    drifted = check_entity_drift.diff("state", {"A": "1", "B": "2", "C": "3"}, {"A": "1", "C": "9", "D": "4"})
    out = capsys.readouterr().out

    assert drifted is True
    assert "ADDED    'D'" in out and "REMOVED  'B'" in out and "CHANGED  'C'" in out


def test_entity_drift_with_identical_lists_reports_none(capsys):
    assert check_entity_drift.diff("state", {"A": "1"}, {"A": "1"}) is False
    assert "no drift" in capsys.readouterr().out


# --- a killed write must not poison the next append --------------------------------------

def test_a_torn_csv_tail_is_cut_before_the_next_record_is_appended(daily, search, capsys):
    """The job was killed mid-row. Appending straight after it would glue the next
    record onto the stump -- one row of fifteen-odd fields that build_site.py
    cannot read. D9's row was the one cut; it is listed tonight, so it comes
    back as a whole row of its own, beside the new D5."""
    stump = ",".join(csv_row(9)[:6])[:40]
    rows = daily(range(5), [([rec(i) for i in (0, 1, 2, 3, 4, 5, 9)], 1)], tail=stump)

    assert {len(r) for r in rows} == {11}, "every row is a whole row"
    assert [r[0] for r in rows] == ["D0", "D1", "D2", "D3", "D4", "D5", "D9"]
    assert status_of(rows, 5) == ["Active"] and status_of(rows, 9) == ["Active"]
    assert "ends mid-row" in capsys.readouterr().out
    assert stamp_written(daily.tmp)


def test_both_entry_points_repair_the_tail_before_they_append():
    """The wiring, pinned at the source: --daily and the full scrape both append."""
    import inspect

    assert "repair_csv_tail(output_csv)" in inspect.getsource(scrape.run_daily)
    assert "repair_csv_tail(output_csv)" in inspect.getsource(scrape.main)
