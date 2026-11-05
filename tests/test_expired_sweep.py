"""--sweep-expired: the Expired records the Active-only nightly never saw (audit n13).

The sweep reads an entity's Expired search newest-first by Begin Date and appends what the
CSV has never held, down to the first page that begins before its window. What it rests on,
pinned here: every request repeats the sort (the pager's own links drop it), the stop rule
trusts the order only while the order holds, it only ever appends, and each row it appends
goes into the flip ledger so that a purchase order re-opened later is a return, not a stray.

Nothing here touches the network or the repository's data/: the search, the token and the
document fetches are scripted, and documents.jsonl is not written.
"""

import csv
import datetime
import json
import os
import sys
import types

import pytest
import requests

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import scrape  # noqa: E402

ENTITY = "Chadron State College"
OTHER = "Peru State College"
SINCE = datetime.date(2026, 8, 1)
CSV = os.path.join("data", "nu_purchase_orders.csv")
LEDGER = os.path.join("data", "purchase-order_daily_flips.json")


class Response:
    text = "<html></html>"

    def raise_for_status(self):
        pass


class Session:
    """Counts what the sweep asks of the server: searches (POST) and pages (GET)."""

    def __init__(self):
        self.posts = 0
        self.gets = []

    def post(self, *args, **kwargs):
        self.posts += 1
        return Response()

    def get(self, url, *args, **kwargs):
        self.gets.append(url)
        return Response()


def rec(i, begin="10/01/2026", entity=ENTITY):
    return {"doc_number": f"P{i}", "doc_type": "PO", "entity_code": "050", "entity_name": entity.upper(),
            "vendor": f"VENDOR {i}", "amount": "$1.00", "begin_date": begin, "end_date": begin,
            "detail_url": f"https://example.test/Search/SearchDocuments?DN=n{i}&V=v{i}"}


def csv_row(i, status="Active", entity=ENTITY):
    return [f"P{i}", "PO", "050", entity, f"VENDOR {i}", "$1.00", "10/01/2026", "10/01/2026", status,
            f"https://example.test/Search/SearchDocuments?DN=n{i}&V=v{i}", ""]


@pytest.fixture
def search(monkeypatch):
    """Script the server: `pages` are what parse_results_page returns, one entry per page
    the sweep reads, as (records, last_page). A page that is an exception is raised."""
    fetched = []

    def script(pages):
        it = iter(pages)

        def parse(_soup):
            page = next(it)
            if isinstance(page, Exception):
                raise page
            return page

        monkeypatch.setattr(scrape, "parse_results_page", parse)
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


def sweep(session, known=(), since=SINCE, **kwargs):
    pages = []
    known = {scrape.document_key(rec(i)["detail_url"]) for i in known}
    result = scrape.sweep_entity(session, ENTITY, "1", "Higher Education", "Purchase Order", since,
                                 known, lambda rows, entries: pages.append((rows, entries)), **kwargs)
    return result, pages, known


# --- the pieces ---------------------------------------------------------------------

def test_since_is_a_date_or_a_number_of_days_back():
    assert scrape.parse_since("2026-08-01") == SINCE
    assert scrape.parse_since("35", today=datetime.date(2026, 11, 5)) == datetime.date(2026, 10, 1)
    with pytest.raises(ValueError):
        scrape.parse_since("August")


def test_order_is_newest_first_ties_and_unreadable_dates_allowed():
    d = datetime.date
    assert scrape.out_of_order([d(2026, 10, 3), d(2026, 10, 3), None, d(2026, 10, 1)], None) is None
    assert scrape.out_of_order([d(2026, 9, 30)], d(2026, 10, 1)) is None
    assert "10/02/2026 follows 10/01/2026" in scrape.out_of_order([d(2026, 10, 1), d(2026, 10, 2)], None)


def test_a_page_that_begins_after_the_last_one_ended_is_out_of_order():
    """What the site falling back to its own order looks like across a page boundary."""
    d = datetime.date
    assert "10/05/2026 follows 10/01/2026" in scrape.out_of_order([d(2026, 10, 5)], d(2026, 10, 1))


def test_known_keys_cover_every_status(tmp_path):
    path = tmp_path / "po.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(scrape.CSV_HEADER)
        w.writerows([csv_row(1), csv_row(2, "Expired")])
    assert scrape.load_known_keys(str(path)) == {scrape.document_key(csv_row(i)[9]) for i in (1, 2)}


# --- sweep_entity ----------------------------------------------------------------

def test_every_page_request_repeats_the_newest_first_sort(search):
    """The pager's links are a bare ?page=N, which falls back to the search's own order."""
    session = search([([rec(1, "10/03/2026")], 3), ([rec(2, "10/02/2026")], 3), ([rec(3, "10/01/2026")], 3)])
    (appended, finished, problem), _, _ = sweep(session)

    assert (appended, finished, problem) == (3, True, None)
    assert session.posts == 1
    assert session.gets == [f"{scrape.RESULTS_URL}?page={n}&{scrape.SWEEP_SORT}" for n in (1, 2, 3)]


def test_it_stops_at_the_first_page_that_begins_before_its_window_and_keeps_that_page(search, capsys):
    """A page straddling the boundary is read on; the first page wholly before it is the
    last one read, and its records still count -- a record the window missed by a day is
    still a record the CSV lacks."""
    session = search([([rec(1, "08/03/2026"), rec(2, "07/31/2026")], 9), ([rec(3, "07/30/2026")], 9)])
    (appended, finished, problem), pages, _ = sweep(session)

    assert (appended, finished, problem) == (3, True, None)
    assert len(session.gets) == 2, "page 3 of 9 is never asked for"
    assert "begins before 08/01/2026" in capsys.readouterr().out
    assert [r[0] for rows, _ in pages for r in rows] == ["P1", "P2", "P3"]


def test_new_records_are_appended_as_expired_and_known_ones_cost_nothing(search):
    session = search([([rec(1), rec(2), rec(3), rec(2)], 1)])
    (appended, finished, _), pages, known = sweep(session, known=[1])

    assert (appended, finished) == (2, True)
    assert search.fetched == [["P2", "P3"]], "the known record is not fetched; a repeat once"
    [(rows, entries)] = pages
    assert [(r[0], r[3], r[8]) for r in rows] == [("P2", ENTITY, "Expired"), ("P3", ENTITY, "Expired")]
    assert [r[10] for r in rows] == [scrape.view_url_for([{"token": t}]) for t in ("TP2", "TP3")]
    assert len(entries) == 2
    assert scrape.document_key(rec(3)["detail_url"]) in known, "a later page will not append it again"


def test_records_whose_detail_pages_cannot_be_read_are_left_for_the_next_run(search, monkeypatch, capsys):
    monkeypatch.setattr(scrape, "fetch_documents_parallel", lambda records: [None, [{"token": "T"}]])
    session = search([([rec(1), rec(2)], 1)])
    (appended, finished, _), pages, known = sweep(session)

    assert (appended, finished) == (1, True)
    assert [r[0] for r in pages[0][0]] == ["P2"]
    assert scrape.document_key(rec(1)["detail_url"]) not in known
    assert "left for the next run" in capsys.readouterr().out


def test_an_order_it_cannot_trust_stops_it_without_claiming_completion(search):
    """Without the order, "this page is before the window" says nothing about the pages
    after it, so the sweep neither stops early calling that complete nor reads on."""
    session = search([([rec(1, "10/03/2026"), rec(2, "10/01/2026")], 9), ([rec(3, "10/02/2026")], 9)])
    (appended, finished, problem), pages, _ = sweep(session)

    assert (appended, finished) == (2, False)
    assert problem == "page 2: Begin Date 10/02/2026 follows 10/01/2026"
    assert [r[0] for rows, _ in pages for r in rows] == ["P1", "P2"], "the bad page appends nothing"
    assert len(session.gets) == 2


def test_an_empty_first_page_is_a_clean_finish(search, capsys):
    session = search([([], None)])
    assert sweep(session)[0] == (0, True, None)
    assert "No results found" in capsys.readouterr().out


def test_a_blank_page_mid_run_is_re_searched_once_and_the_sweep_resumes(search):
    session = search([([rec(1, "10/02/2026")], 2), ([], None), ([rec(2, "10/01/2026")], 2)])
    assert sweep(session)[0] == (2, True, None)
    assert session.posts == 2
    assert session.gets[-1] == f"{scrape.RESULTS_URL}?page=2&{scrape.SWEEP_SORT}"


def test_a_page_still_blank_after_the_re_search_stops_unfinished(search, capsys):
    session = search([([rec(1)], 5), ([], None), ([], None)])
    assert sweep(session)[0] == (1, False, None)
    assert session.posts == 2
    assert "not finished" in capsys.readouterr().out


def test_a_deadline_stops_it_unfinished_after_the_page_in_hand(search):
    session = search([([rec(1)], 5)])
    (appended, finished, _), pages, _ = sweep(session, deadline=1)

    assert (appended, finished) == (1, False)
    assert len(pages) == 1, "the page in hand is written before it stops"


# --- run_sweep, on a CSV --------------------------------------------------------------

@pytest.fixture
def swept(monkeypatch, tmp_path, search):
    """One `scrape.py purchase-order --sweep-expired 2026-08-01` over a CSV holding `known`."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(scrape, "document_service_healthy", lambda: True)
    monkeypatch.setattr(scrape, "HIGHER_ED_ENTITIES", {ENTITY: "1", OTHER: "2"})
    written = []
    monkeypatch.setattr(scrape, "append_documents",
                        lambda entries, path=None: written.extend(entries) or len(entries))

    def run(known, pages, entity=None, since="2026-08-01", hours=None):
        if known is not None and not os.path.exists(CSV):
            with open(CSV, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(scrape.CSV_HEADER)
                w.writerows(csv_row(i, status) for i, status in known)
        session = search(pages)
        monkeypatch.setattr(scrape, "build_session", lambda: session)
        scrape.run_sweep(types.SimpleNamespace(dataset="purchase-order", entity=entity,
                                               sweep_expired=since, hours=hours))
        with open(CSV, newline="", encoding="utf-8") as f:
            return [r for r in csv.reader(f)][1:]

    run.written = written
    return run


def ledger():
    with open(LEDGER, encoding="utf-8") as f:
        return json.load(f)


def test_it_appends_only_and_records_each_row_in_the_flip_ledger(swept):
    rows = swept([(1, "Active"), (2, "Expired")],
                 [([rec(1), rec(2), rec(3)], 1),            # ENTITY: 3 is new
                  ([rec(4, entity=OTHER)], 1)],              # OTHER: 4 is new
                 entity=[ENTITY, OTHER])

    assert rows[:2] == [csv_row(1), csv_row(2, "Expired")], "rows it already held are untouched"
    assert [(r[0], r[3], r[8]) for r in rows[2:]] == [("P3", ENTITY, "Expired"), ("P4", OTHER, "Expired")]
    today = datetime.date.today().isoformat()
    assert ledger() == {scrape.ledger_key(rec(i)["detail_url"]): today for i in (3, 4)}
    assert len(swept.written) == 2, "one documents entry per row, so the two files agree"


def test_a_second_sweep_over_the_same_listing_appends_nothing(swept, search):
    listing = [([rec(1), rec(3)], 1)]
    swept([(1, "Active")], listing, entity=[ENTITY])
    rows = swept(None, listing, entity=[ENTITY])

    assert [r[0] for r in rows] == ["P1", "P3"]
    assert search.fetched == [["P3"]], "the second run fetches nothing"


def test_it_refuses_a_dataset_it_has_no_rows_for(swept, capsys):
    with pytest.raises(SystemExit) as e:
        swept([], [])
    assert e.value.code == 1
    assert "has no records" in capsys.readouterr().err


def test_it_refuses_while_the_state_serves_no_documents(swept, monkeypatch, capsys):
    monkeypatch.setattr(scrape, "document_service_healthy", lambda: False)
    with pytest.raises(SystemExit) as e:
        swept([(1, "Active")], [])
    assert e.value.code == 1
    assert "Nothing was swept" in capsys.readouterr().err


def test_an_unknown_entity_is_refused_before_anything_is_read(swept):
    with pytest.raises(SystemExit) as e:
        swept([(1, "Active")], [], entity=["Univ of Nowhere"])
    assert e.value.code == 2


def test_an_order_problem_fails_the_run_and_keeps_what_was_appended(swept, capsys):
    with pytest.raises(SystemExit) as e:
        swept([(1, "Active")],
              [([rec(2, "10/03/2026"), rec(3, "10/01/2026")], 9), ([rec(4, "10/02/2026")], 9)],
              entity=[ENTITY])
    assert e.value.code == 1
    assert "::error title=Expired sweep stopped::" in capsys.readouterr().out
    with open(CSV, newline="", encoding="utf-8") as f:
        assert [r[0] for r in csv.reader(f)][1:] == ["P1", "P2", "P3"]
    assert len(ledger()) == 2


def test_a_failing_state_site_leaves_the_entity_unfinished_not_the_run_red(swept, capsys):
    """The session has already retried; the next run reads the entity again."""
    rows = swept([(1, "Active")],
                 [([rec(2, "10/03/2026")], 9), requests.ConnectionError("reset"),
                  ([rec(5, entity=OTHER)], 1)],
                 entity=[ENTITY, OTHER])

    assert [r[0] for r in rows] == ["P1", "P2", "P5"]
    out = capsys.readouterr().out
    assert "::warning title=Expired sweep unfinished::purchase-order: not finished: " + ENTITY in out
    assert "2 Expired record(s) appended" in out
    assert len(ledger()) == 2


def test_the_time_limit_leaves_the_rest_unfinished(swept, monkeypatch, capsys):
    rows = swept([(1, "Active")], [], entity=[ENTITY, OTHER], hours=-1)
    assert [r[0] for r in rows] == ["P1"]
    assert f"not finished: {ENTITY}, {OTHER}" in capsys.readouterr().out


# --- a swept record re-opened later is a return, not a stray -------------------------------

def test_a_swept_purchase_order_listed_active_later_flips_back_without_a_leak(swept, search, monkeypatch):
    """Six swept records listed Active the next night: without their ledger entries that
    is six strays, over LEAK_FLOOR, and the entity's flip-backs would be held as a leak."""
    swept([(i, "Active") for i in range(4)], [([rec(i) for i in range(10, 16)], 1)], entity=[ENTITY])
    monkeypatch.setattr(scrape, "HIGHER_ED_ENTITIES", {ENTITY: "1"})
    session = search([([rec(i) for i in list(range(4)) + list(range(10, 16))], 1)])
    monkeypatch.setattr(scrape, "build_session", lambda: session)
    scrape.run_daily(types.SimpleNamespace(dataset="purchase-order", entity=[ENTITY], hours=None))

    with open(CSV, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.reader(f)][1:]
    assert len(rows) == 10, "flipped back in place"
    assert {r[8] for r in rows} == {"Active"}
    with open(os.path.join("data", "daily_diff_report.json"), encoding="utf-8") as f:
        [entry] = json.load(f)
    assert entry["entities"][ENTITY]["flipped_back"] == 6
    assert ledger() == {}


# --- the command line ---------------------------------------------------------------------

def parse(monkeypatch, *argv):
    called = []
    monkeypatch.setattr(scrape, "run_sweep", called.append)
    monkeypatch.setattr(sys, "argv", ["scrape.py", *argv])
    scrape.main()
    return called


def test_a_bare_sweep_takes_the_nightly_window(monkeypatch):
    [args] = parse(monkeypatch, "purchase-order", "--sweep-expired", "--hours", "0.25")
    assert args.sweep_expired == str(scrape.SWEEP_DAYS) and args.hours == 0.25


def test_a_sweep_takes_a_date(monkeypatch):
    [args] = parse(monkeypatch, "purchase-order", "--sweep-expired", "2026-08-01")
    assert args.sweep_expired == "2026-08-01"


@pytest.mark.parametrize("argv", [
    ["--sweep-expired", "--daily"],
    ["--sweep-expired", "--fresh"],
    ["--sweep-expired", "last-august"],
])
def test_a_sweep_refuses_what_it_cannot_honour(monkeypatch, argv):
    with pytest.raises(SystemExit) as e:
        parse(monkeypatch, "purchase-order", *argv)
    assert e.value.code == 2
