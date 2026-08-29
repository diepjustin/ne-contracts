"""Telling "this contract has no document" apart from "we could not find out".

On 17-18 Aug 2026 the state's document service went down for about two days.
Detail pages kept returning HTTP 200 and simply stopped rendering the link to
the file -- which is byte-for-byte what a contract with no document attached
looks like. `get_view_url` returned "" for both, so a scrape run during the
outage would have recorded "no document" as a fact for every new contract, and
published a site with silently missing links. Nothing would have failed.

The outage happened to coincide with the state's search also returning nothing,
so no rows were written and no damage was done. These tests pin the behaviour
that means we would not need that luck a second time.
"""

import os
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import scrape  # noqa: E402


def doc_row(name, size, token):
    """One file's row as the state renders it: the name links to the document,
    and so do the View and Download controls beside it -- three links, one file."""
    return (f'<tr><td><a href="/Search/ViewDocument?D={token}">{name}</a></td>'
            f'<td>{size}</td>'
            f'<td><a href="/Search/ViewDocument?D={token}">View</a> '
            f'<a href="/Search/DownloadDocument?D={token}">Download</a></td></tr>')


def documents_page(*rows):
    return ('<html><body><h2>Document Results</h2><table>'
            '<tr><th>File Name</th><th>File Size</th><th></th></tr>'
            + "".join(rows) + '</table></body></html>')


LINK_PAGE = documents_page(doc_row("DOC1738782367", "150Kb", "aVw1%3D%3D"))

# Shaped after UNL's Axon contract CW33053, which publishes nine documents
# where this project captured one. See README.md, "Things that bit us".
MULTI_PAGE = documents_page(doc_row("DOC2061661286", "2Mb", "eJUl%3D%3D"),
                            doc_row("DOC2061539553", "8Mb", "owmB%3D%3D"),
                            doc_row("DOC2074267252", "10Mb", "DDYa%3D%3D"))

NO_LINK_PAGE = '<html><body><p>Documents not available for immediate viewing.</p></body></html>'


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeSession:
    """Stands in for the detail session. `pages` maps URL -> body, or an
    exception instance to raise for that URL."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        page = self.pages.get(url, NO_LINK_PAGE)
        if isinstance(page, Exception):
            raise page
        return FakeResponse(page)


def use_session(monkeypatch, pages):
    session = FakeSession(pages)
    monkeypatch.setattr(scrape, "detail_session", lambda: session)
    monkeypatch.setattr(scrape.time, "sleep", lambda _s: None)
    return session


# --- get_view_url: three outcomes, not two ---------------------------------

def test_page_offering_a_document_returns_its_url(monkeypatch):
    use_session(monkeypatch, {"u": LINK_PAGE})
    assert scrape.get_view_url("u") == scrape.BASE_URL + "/Search/ViewDocument?D=aVw1%3D%3D"


def test_clean_page_without_a_link_returns_empty_string(monkeypatch):
    """A real fact: we read the page and it offers nothing."""
    use_session(monkeypatch, {"u": NO_LINK_PAGE})
    assert scrape.get_view_url("u") == ""


def test_failed_fetch_returns_none_not_empty_string(monkeypatch):
    """The bug this whole file exists for. A network failure is not evidence
    that a contract has no document, and must not be recorded as though it were."""
    use_session(monkeypatch, {"u": RuntimeError("connection reset")})
    assert scrape.get_view_url("u") is None


def test_missing_detail_url_is_a_real_absence(monkeypatch):
    """No detail URL at all is genuinely nothing to view -- not an unknown."""
    use_session(monkeypatch, {})
    assert scrape.get_view_url("") == ""


# --- get_documents: every document, not just the first ---------------------

def test_every_document_on_the_page_is_captured(monkeypatch):
    """The bug Justin found: `find` kept document 1 of 9 and dropped the rest."""
    use_session(monkeypatch, {"u": MULTI_PAGE})
    assert [d["name"] for d in scrape.get_documents("u")] == [
        "DOC2061661286", "DOC2061539553", "DOC2074267252"]


def test_documents_keep_the_states_order(monkeypatch):
    """The state's order is not date order, and it decides which document is
    primary -- so it is data, not presentation, and must not be re-sorted."""
    use_session(monkeypatch, {"u": MULTI_PAGE})
    assert [d["token"] for d in scrape.get_documents("u")] == [
        "eJUl%3D%3D", "owmB%3D%3D", "DDYa%3D%3D"]


def test_the_states_own_name_and_size_are_kept_verbatim(monkeypatch):
    """Both are the state's values. A reader checks our list against the source
    with them, so they are reproduced rather than reformatted."""
    use_session(monkeypatch, {"u": MULTI_PAGE})
    assert scrape.get_documents("u")[2] == {
        "name": "DOC2074267252", "size": "10Mb", "token": "DDYa%3D%3D"}


def test_one_file_with_three_links_counts_once(monkeypatch):
    """Each row carries the name, View and Download pointing at the same file.
    Counting links instead of files would have trebled every count."""
    use_session(monkeypatch, {"u": LINK_PAGE})
    assert len(scrape.get_documents("u")) == 1


def test_the_primary_document_is_the_one_the_csv_already_holds(monkeypatch):
    """Continuity, and the reason descriptions survive this change.
    doc_text.jsonl and scope.jsonl are keyed by the view token, so the first
    document must stay the first document or every description is orphaned."""
    use_session(monkeypatch, {"u": MULTI_PAGE})
    documents = scrape.get_documents("u")
    assert scrape.view_url_for(documents) == scrape.get_view_url("u")
    assert scrape.view_url_for(documents).endswith("D=eJUl%3D%3D")


def test_a_clean_page_offering_nothing_is_an_empty_list(monkeypatch):
    """A real fact, and distinct from the None below."""
    use_session(monkeypatch, {"u": NO_LINK_PAGE})
    assert scrape.get_documents("u") == []


def test_a_failed_fetch_is_none_not_an_empty_list(monkeypatch):
    """The 17 Aug 2026 signature in its new shape. An empty list says the state
    publishes nothing; None says we could not find out. Collapsing them is how
    an outage gets recorded as fact."""
    use_session(monkeypatch, {"u": RuntimeError("connection reset")})
    assert scrape.get_documents("u") is None


def test_a_page_whose_rows_stop_being_file_rows_yields_nothing():
    """A redesign that drops the file table should read as "no documents" and
    be caught by the canaries -- never as a silently smaller count."""
    assert scrape.parse_documents('<html><body><table>'
                                  '<tr><td>Something else entirely</td><td>x</td></tr>'
                                  '</table></body></html>') == []


# --- document_service_healthy: the canary ----------------------------------

def test_one_working_canary_means_the_service_is_up(monkeypatch):
    urls = list(scrape.CANARY_DOCUMENTS.values())
    use_session(monkeypatch, {urls[0]: NO_LINK_PAGE,
                              urls[1]: LINK_PAGE,
                              urls[2]: NO_LINK_PAGE})
    assert scrape.document_service_healthy() is True


def test_every_canary_losing_its_document_means_the_service_is_down(monkeypatch):
    """The 17 Aug signature: pages read fine, documents we know exist are gone."""
    use_session(monkeypatch, {u: NO_LINK_PAGE for u in scrape.CANARY_DOCUMENTS.values()})
    assert scrape.document_service_healthy() is False


def test_unreachable_canaries_do_not_condemn_the_state(monkeypatch):
    """A canary we cannot fetch proves nothing -- the URL may simply have rotted.
    Biased towards True so a stale canary cannot silently halt every run."""
    use_session(monkeypatch, {u: RuntimeError("timed out")
                              for u in scrape.CANARY_DOCUMENTS.values()})
    assert scrape.document_service_healthy() is True


def test_a_canary_that_errors_does_not_mask_a_real_outage(monkeypatch):
    """One unreachable canary, two that read clean and offer nothing: still down."""
    urls = list(scrape.CANARY_DOCUMENTS.values())
    use_session(monkeypatch, {urls[0]: RuntimeError("timed out"),
                              urls[1]: NO_LINK_PAGE,
                              urls[2]: NO_LINK_PAGE})
    assert scrape.document_service_healthy() is False


def test_healthy_check_stops_at_the_first_working_document(monkeypatch):
    """Costs one request when the state is up, which is the normal case."""
    urls = list(scrape.CANARY_DOCUMENTS.values())
    session = use_session(monkeypatch, {u: LINK_PAGE for u in urls})
    assert scrape.document_service_healthy() is True
    assert len(session.requested) == 1


# --- the writer loop: an unknown record must not become a row ---------------

class Recorder:
    """Stands in for csv.writer."""

    def __init__(self):
        self.rows = []

    def writerow(self, row):
        self.rows.append(row)


def a_record(doc, detail):
    return {"doc_number": doc, "doc_type": "CN", "entity_code": "051",
            "vendor": "ACME", "amount": "$1.00", "begin_date": "01/01/2020",
            "end_date": "12/31/2026", "detail_url": detail}


def drive_one_page(monkeypatch, records, view_urls, tmp_path=None):
    """Run scrape_entity over a single fabricated results page.

    `view_urls` maps detail URL -> what the detail fetch should hand back, in
    the same three-way vocabulary the real one uses. A URL string stands for a
    record offering that one document, "" for one offering none, and None for
    a detail page we could not read at all."""
    def as_documents(url):
        if url is None:
            return None
        return [{"name": "DOC1", "size": "1Mb", "token": url.split("D=")[-1]}] if url else []

    monkeypatch.setattr(scrape, "get_token", lambda _s: "token")
    monkeypatch.setattr(scrape.time, "sleep", lambda _s: None)
    monkeypatch.setattr(scrape, "parse_results_page", lambda _soup: (records, 1))
    monkeypatch.setattr(scrape, "fetch_documents_parallel",
                        lambda rs: [as_documents(view_urls[r["detail_url"]]) for r in rs])
    # Keep the log out of data/ -- these tests fabricate records, and this file
    # is real scrape output that build_site.py reads.
    monkeypatch.setattr(scrape, "append_documents", lambda entries, path=None: len(entries))

    class Session:
        def post(self, *a, **k):
            return FakeResponse("<html></html>")

        def get(self, *a, **k):
            return FakeResponse("<html></html>")

    writer = Recorder()
    scrape.scrape_entity(Session(), "Test Entity", "1", "Active", "E", "CN", writer)
    return writer.rows


def test_a_record_we_could_not_read_is_not_written_at_all(monkeypatch):
    """The whole point. An unreadable detail page must leave no row behind,
    because a row with a blank View URL asserts the contract has no document."""
    records = [a_record("A", "url-a"), a_record("B", "url-b")]
    rows = drive_one_page(monkeypatch, records, {"url-a": "", "url-b": None})
    assert [r[0] for r in rows] == ["A"]


def test_a_genuine_absence_is_still_written(monkeypatch):
    """The guard must not swallow real facts: "" means we looked and there is
    nothing, and that row belongs in the CSV with an empty View URL."""
    records = [a_record("A", "url-a")]
    rows = drive_one_page(monkeypatch, records, {"url-a": ""})
    assert len(rows) == 1
    assert rows[0][-1] == ""


def test_a_document_url_lands_in_the_last_column(monkeypatch):
    """The CSV's last column stays one URL -- the record's primary document --
    even now that the full list is captured. Every downstream consumer of these
    files, build_site.py included, still reads exactly one View URL per row."""
    records = [a_record("A", "url-a")]
    view = scrape.BASE_URL + "/Search/ViewDocument?D=aVw1%3D%3D"
    rows = drive_one_page(monkeypatch, records, {"url-a": view})
    assert rows[0][-1] == view


# --- the same distinction, one layer down in extract_text --------------------
#
# scrape.py learned this lesson in Aug 2026 and extract_text.py did not. It
# fetches documents on its own and classifies what comes back, and two of its
# four verdicts are permanent -- so an outage answering every request with an
# error page could write "the state has no file" across the corpus, unretried.
# On 22 Aug 2026 the state did exactly that, from a healthy-looking HTTP 200.

import extract_text  # noqa: E402


class FakeDocResponse:
    """Enough of a requests.Response for classify_non_pdf."""

    def __init__(self, body, content_type, text=None):
        self.content = body
        self.headers = {"Content-Type": content_type}
        self.text = body.decode("utf-8", "replace") if text is None else text


# The real body the state served on 22 Aug 2026, trimmed.
OUTAGE_PAGE = (b"<html><body><h1>Error</h1><p>An internal error occured: An error "
               b"occurred within the Unity API: The type initializer for "
               b"'Hyland.Core.CoreUtility' threw an exception.</p></body></html>")


def test_an_error_page_is_retryable_not_an_absence():
    """The whole point. "unavailable" is never asked about again, so an outage
    recorded as one is permanent."""
    result = extract_text.classify_non_pdf(
        FakeDocResponse(OUTAGE_PAGE, "text/html; charset=utf-8"))
    assert result["status"] == "error"
    assert result["status"] != "unavailable"


def test_the_error_page_is_kept_so_it_can_be_recognised_later():
    """14,185 documents already carry a non-PDF verdict with no body kept, so
    there is no way to tell which were written during an outage. Not again."""
    result = extract_text.classify_non_pdf(
        FakeDocResponse(OUTAGE_PAGE, "text/html; charset=utf-8"))
    assert "Hyland.Core.CoreUtility" in result["bodyHead"]
    assert len(result["bodySha256"]) == 64


def test_two_outage_pages_hash_alike():
    """Which is what makes "they all served the identical error" one line in a
    report rather than 14,000 separate mysteries."""
    a = extract_text.classify_non_pdf(FakeDocResponse(OUTAGE_PAGE, "text/html"))
    b = extract_text.classify_non_pdf(FakeDocResponse(OUTAGE_PAGE, "text/html"))
    assert a["bodySha256"] == b["bodySha256"]


def test_a_tiff_is_a_document_we_cannot_read_not_a_missing_one():
    """17,991 of these. The state published a file; we cannot parse it. Saying
    it has no document blames the state for our own format support."""
    result = extract_text.classify_non_pdf(
        FakeDocResponse(b"II*\x00 tiff bytes", "image/tiff"))
    assert result["status"] == "unsupported"
    assert result["contentType"] == "image/tiff"


def test_a_word_document_is_also_a_file():
    result = extract_text.classify_non_pdf(
        FakeDocResponse(b"PK\x03\x04", "application/vnd.openxmlformats-officedocument"
                                       ".wordprocessingml.document"))
    assert result["status"] == "unsupported"


def test_anything_that_is_not_a_recognised_file_stays_retryable():
    """classify_non_pdf never sees a 404 -- fetch_and_extract answers that one
    before it gets here -- so everything reaching this function arrived as a
    200, and nothing in a 200 justifies a permanent absence."""
    assert extract_text.classify_non_pdf(
        FakeDocResponse(b"nope", "application/octet-stream"))["status"] == "error"


def test_an_unsupported_file_carries_no_body_excerpt():
    """The excerpt exists to identify error pages. A TIFF's first 300 bytes
    decoded as text is noise, and would land in an append-only log."""
    result = extract_text.classify_non_pdf(
        FakeDocResponse(b"II*\x00 tiff bytes", "image/tiff"))
    assert "bodyHead" not in result


def test_a_404_stays_a_settled_answer():
    """The one verdict that should never be re-asked. It was an answer."""
    assert not extract_text.never_proven_absent(
        {"status": "unavailable", "detail": "HTTP 404"})


def test_a_non_pdf_200_was_never_an_answer():
    """A 200 carrying HTML proves nothing about whether a file exists, and
    14,185 of these are on disk with no body kept to tell them apart."""
    assert extract_text.never_proven_absent(
        {"status": "unavailable", "detail": "non-PDF response (text/html; charset=utf-8)"})


def test_a_tiff_recorded_as_unavailable_is_re_asked_to_be_relabelled():
    """It will come back the same way. The point is that the corpus stops
    calling 17,991 documents missing when the state published every one."""
    assert extract_text.never_proven_absent(
        {"status": "unavailable", "detail": "non-PDF response (image/tiff)"})


def test_documents_we_read_are_left_alone():
    for status in ("text", "scanned", "error", "unsupported"):
        assert not extract_text.never_proven_absent({"status": status, "detail": ""})
