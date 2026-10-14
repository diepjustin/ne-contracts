"""parse_results_page on HTML, and scrape_entity driven by it.

Every other test replaced the parser with a lambda, so the function that turns the
state's markup into records -- and the only place a changed column, a renamed table
id or a pager the scraper cannot read would show -- was never given a page. These
feed it real-shaped HTML.

The markup here mirrors the structure of a page captured from the state's site in
March 2026 (table id, classes, the eight sortable header cells, one anchor in the
first cell, a tfoot pager with page=N links and Next / Last); the rows are
invented. The State grid is assumed to differ from it only in its table id, which
is what the parser's own comment says and the one thing not captured. Fixtures
freeze today's markup, so they guard against our own edits to the parser and the
recovery loop, not against the state changing its page.

Nothing here touches the network or data/.
"""

import csv
import io
import os
import sys

import pytest
from bs4 import BeautifulSoup

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import scrape  # noqa: E402

HEADERS = [("DocNumber", "Doc Number"), ("DocType", "Type"), ("EntityNumber", "Entity"),
           ("EntityName", "Entity Name"), ("VendorName", "Vendor"), ("ContractAmount", "Amount"),
           ("BeginDate", "Begin Date"), ("EndDate", "End Date")]

THEAD = ('<thead><tr class="table_headerRow">' + "".join(
    f'<th scope="col"><a data-swhglnk="true" href="/Search/SearchResults?sort={key}&amp;sortdir=ASC">'
    f"{label}</a></th>" for key, label in HEADERS) + "</tr></thead>")


def detail_href(i):
    """The first cell's href as the page writes it: percent-encoded values, & as &amp;."""
    return ("/Search/SearchDocuments?A=aaa%3D%3D&amp;D=ddd%3D%3D&amp;DN=dn%2B{i}%3D%3D"
            "&amp;N=nnn%3D&amp;DT=dt%3D%3D&amp;V=vv%2F{i}%3D").format(i=i)


def expected_detail(i):
    return (scrape.BASE_URL + "/Search/SearchDocuments?A=aaa%3D%3D&D=ddd%3D%3D&DN=dn%2B{i}%3D%3D"
            "&N=nnn%3D&DT=dt%3D%3D&V=vv%2F{i}%3D").format(i=i)


def result_row(i, extra=0, link=True, cells=8):
    """One grid row for record i, with `extra` surplus cells, or only `cells` cells."""
    first = (f'<a href="{detail_href(i)}">CW9{i:04d}</a>' if link else f"  CW9{i:04d} ")
    row = [f'<td class="searchresultgridcell">{first}</td>',
           '<td class="searchresultgridcell"> CN </td>',
           '<td class="searchresultgridcell">051</td>',
           '<td class="searchresultgridcell">University of Nebraska Lincoln</td>',
           f'<td class="searchresultgridcell">EXAMPLE VENDOR {i}</td>',
           '<td class="searchresultgridcellright">$1,234.50</td>',
           '<td class="searchresultgridcellcenter">11/05/2026</td>',
           '<td class="searchresultgridcellcenter">11/07/2026</td>']
    row += ['<td class="searchresultgridcell">extra</td>'] * extra
    return "<tr>" + "".join(row[:cells]) + "</tr>"


def pager(*hrefs, labels=None):
    links = "".join(f'<a data-swhglnk="true" href="{h}">{(labels or {}).get(h, h[-3:])}</a> ' for h in hrefs)
    return f'<tfoot><tr class="table_headerRow"><td colspan="8">1 {links}</td></tr></tfoot>'


REAL_PAGER = pager("/Search/SearchResults?page=2", "/Search/SearchResults?page=3",
                   "/Search/SearchResults?page=2", "/Search/SearchResults?page=217")


def page(rows=(), table_id="entitygrid", foot=REAL_PAGER, head=THEAD, body=True, extra_before=""):
    inner = "".join(rows)
    inner = f"<tbody>{inner}</tbody>" if body else inner
    return (f"<html><body>{extra_before}<div id=\"grid_container\">"
            f'<table class="table_grid" data-swhgajax="true" id="{table_id}">{head}{inner}{foot}</table>'
            "</div></body></html>")


def parse(html):
    return scrape.parse_results_page(BeautifulSoup(html, "html.parser"))


# --- the records ---------------------------------------------------------------------------

def test_a_real_shaped_page_parses_every_field_into_its_own_place():
    records, last = parse(page([result_row(1), result_row(2)]))

    assert last == 217
    assert records[0] == {
        "doc_number": "CW90001", "doc_type": "CN", "entity_code": "051",
        "entity_name": "University of Nebraska Lincoln", "vendor": "EXAMPLE VENDOR 1",
        "amount": "$1,234.50", "begin_date": "11/05/2026", "end_date": "11/07/2026",
        "detail_url": expected_detail(1)}
    assert [r["doc_number"] for r in records] == ["CW90001", "CW90002"]
    assert records[1]["detail_url"] == expected_detail(2)


def test_the_state_grid_parses_exactly_like_the_higher_education_one():
    rows = [result_row(1), result_row(2)]
    assert parse(page(rows, table_id="agencygrid")) == parse(page(rows, table_id="entitygrid"))
    assert len(parse(page(rows, table_id="agencygrid"))[0]) == 2


def test_the_link_in_the_first_cell_is_the_detail_url_with_its_ampersands_decoded():
    (record,), _ = parse(page([result_row(7)]))
    assert "&amp;" not in record["detail_url"] and record["detail_url"].count("&") == 5
    assert record["detail_url"].startswith(scrape.BASE_URL + "/Search/SearchDocuments?A=")


def test_a_document_number_with_no_link_keeps_its_text_and_has_no_detail_url():
    (record,), _ = parse(page([result_row(3, link=False)]))
    assert record["doc_number"] == "CW90003" and record["detail_url"] == ""


def test_whitespace_around_every_cell_is_trimmed():
    cells = ["\n  CW90004 \n", " CN ", " 051\n", "  University of Nebraska Lincoln  ", "\t EXAMPLE VENDOR 4 ",
             " $1,234.50 ", " 11/05/2026\n", "\n11/07/2026 "]
    row = "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
    (record,), _ = parse(page([row]))
    assert record == {"doc_number": "CW90004", "doc_type": "CN", "entity_code": "051",
                      "entity_name": "University of Nebraska Lincoln", "vendor": "EXAMPLE VENDOR 4",
                      "amount": "$1,234.50", "begin_date": "11/05/2026", "end_date": "11/07/2026",
                      "detail_url": ""}


@pytest.mark.parametrize("cells", [0, 3, 7])
def test_a_row_with_fewer_than_eight_cells_is_skipped_not_fatal(cells):
    """Eight is the floor, not seven: a short row would put the end date in the wrong column."""
    records, _ = parse(page([result_row(1), result_row(2, cells=cells), result_row(3)]))
    assert [r["doc_number"] for r in records] == ["CW90001", "CW90003"]


def test_a_row_of_header_cells_inside_the_body_is_not_a_record():
    header_row = "<tr>" + "".join(f"<th>{label}</th>" for _k, label in HEADERS) + "</tr>"
    records, _ = parse(page([header_row, result_row(1)]))
    assert [r["doc_number"] for r in records] == ["CW90001"]


def test_a_row_with_surplus_cells_is_still_read_from_its_first_eight():
    (record,), _ = parse(page([result_row(5, extra=2)]))
    assert record["end_date"] == "11/07/2026" and record["vendor"] == "EXAMPLE VENDOR 5"


# --- the pager ---------------------------------------------------------------------------------

def test_the_highest_page_number_wins_whatever_order_the_links_come_in():
    first_last = pager("/Search/SearchResults?page=217", "/Search/SearchResults?page=2",
                       "/Search/SearchResults?page=10")
    assert parse(page([result_row(1)], foot=first_last))[1] == 217


@pytest.mark.parametrize("href", ["/Search/SearchResults?page=abc", "/Search/SearchResults?page=",
                                  "/Search/SearchResults?page=2x", "/Search/SearchResults",
                                  "/Search/SearchResults?sort=EndDate&sortdir=ASC"])
def test_a_link_that_is_not_a_page_number_is_ignored(href):
    foot = pager(href, "/Search/SearchResults?page=4")
    assert parse(page([result_row(1)], foot=foot))[1] == 4


def test_an_anchor_with_no_href_is_ignored():
    foot = '<tfoot><tr><td colspan="8">1 <a>2</a> <a href="/Search/SearchResults?page=6">6</a></td></tr></tfoot>'
    assert parse(page([result_row(1)], foot=foot))[1] == 6


def test_a_pager_outside_the_footer_is_not_read():
    stray = '<a href="/Search/SearchResults?page=999">999</a>'
    assert parse(page([result_row(1)], foot=pager("/Search/SearchResults?page=3"), extra_before=stray))[1] == 3


def test_no_footer_and_no_page_links_both_mean_a_single_page():
    assert parse(page([result_row(1)], foot=""))[1] is None
    assert parse(page([result_row(1)], foot=pager()))[1] is None


@pytest.mark.xfail(strict=True, reason=(
    "The page number is read as int(href.split('page=')[-1]), so a link that carries another "
    "parameter after it ('?page=217&sort=BeginDate') raises inside the parser's own try and the "
    "whole pager reads as None -- one page, silently. The state's links are bare today. The "
    "Expired-sweep plan (g1-source-drift-expired-corpus-01) needs sorted links and fixes this with "
    "parse_qs; when it lands this passes, strict=True fails it, and the marker should go."))
def test_a_page_parameter_that_is_not_the_last_one_is_still_read():
    foot = pager("/Search/SearchResults?page=217&sort=BeginDate&sortdir=DESC")
    assert parse(page([result_row(1)], foot=foot))[1] == 217


# --- pages that have no results ------------------------------------------------------------------------

def test_a_page_with_no_grid_has_no_records_and_no_pager():
    assert parse("<html><body><p>No results found</p></body></html>") == ([], None)


def test_a_grid_with_another_id_is_not_read():
    assert parse(page([result_row(1)], table_id="somethingelse")) == ([], None)


def test_a_grid_with_no_tbody_has_no_records_and_no_pager():
    assert parse(page([result_row(1)], body=False)) == ([], None)


def test_an_empty_body_still_reports_its_pager():
    assert parse(page([])) == ([], 217)


# --- scrape_entity, with the real parser -------------------------------------------------------------

class Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class Server:
    """Serves page 1 to a search (POST) and page N to ?page=N (GET). `blank` pages answer
    with an empty grid until the search has been re-run, which is what an expired session does."""

    def __init__(self, pages, blank=(), always_blank=()):
        self.pages, self.blank, self.always_blank = pages, set(blank), set(always_blank)
        self.posts, self.gets = 0, []

    def post(self, url, data=None, **kwargs):
        self.posts += 1
        return Resp(self.pages[1])

    def get(self, url, **kwargs):
        number = int(url.rsplit("page=", 1)[1])
        self.gets.append(number)
        if number in self.always_blank or (number in self.blank and self.posts < 2):
            return Resp(page([], foot=""))
        return Resp(self.pages[number])


@pytest.fixture
def scraped(monkeypatch):
    monkeypatch.setattr(scrape, "get_token", lambda _session: "token")
    monkeypatch.setattr(scrape.time, "sleep", lambda _s: None)
    monkeypatch.setattr(scrape, "append_documents", lambda entries, path=None: len(entries))
    monkeypatch.setattr(scrape, "fetch_documents_parallel", lambda records: [
        [{"name": "DOC", "size": "1Mb", "token": "tok" + r["doc_number"]}] for r in records])

    def run(server, **kwargs):
        out = io.StringIO()
        result = scrape.scrape_entity(server, "Chadron State College", "1", "Active", "Higher Education",
                                      "Contract", csv.writer(out), **kwargs)
        return result, list(csv.reader(io.StringIO(out.getvalue())))

    return run


def two_pages(last=2):
    foot = pager("/Search/SearchResults?page=2", f"/Search/SearchResults?page={last}")
    return {1: page([result_row(1), result_row(2)], foot=foot),
            2: page([result_row(3), result_row(4)], foot=foot)}


def test_two_pages_of_real_markup_become_four_rows_in_the_csv_layout(scraped):
    server = Server(two_pages())
    (total, finished), rows = scraped(server)

    assert (total, finished) == (4, True)
    assert server.posts == 1 and server.gets == [2]
    assert [r[0] for r in rows] == ["CW90001", "CW90002", "CW90003", "CW90004"]
    first = rows[0]
    assert first[1:9] == ["CN", "051", "Chadron State College", "EXAMPLE VENDOR 1", "$1,234.50",
                          "11/05/2026", "11/07/2026", "Active"]
    assert first[9] == expected_detail(1)
    assert first[10] == scrape.BASE_URL + "/Search/ViewDocument?D=tokCW90001"


def test_a_blank_second_page_is_re_searched_and_the_run_still_finishes(scraped):
    server = Server(two_pages(), blank=[2])
    (total, finished), rows = scraped(server)

    assert (total, finished) == (4, True)
    assert server.posts == 2, "the query is re-run once to get the session back"
    assert [r[0] for r in rows][-2:] == ["CW90003", "CW90004"]


def test_a_second_page_that_stays_blank_stops_the_run_without_claiming_it_finished(scraped):
    server = Server(two_pages(), always_blank=[2])
    (total, finished), rows = scraped(server)

    assert (total, finished) == (2, False)
    assert server.posts == 2 and len(rows) == 2


def test_a_search_with_no_results_is_a_clean_finish_with_nothing(scraped):
    server = Server({1: "<html><body>No results found</body></html>"})
    (total, finished), rows = scraped(server)
    assert (total, finished) == (0, True) and rows == [] and server.gets == []
