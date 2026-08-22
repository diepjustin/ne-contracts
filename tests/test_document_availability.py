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


LINK_PAGE = '<html><body><a href="/Home/ViewDocument?id=7">View</a></body></html>'
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
    assert scrape.get_view_url("u") == scrape.BASE_URL + "/Home/ViewDocument?id=7"


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


def drive_one_page(monkeypatch, records, view_urls):
    """Run scrape_entity over a single fabricated results page.

    `view_urls` maps detail URL -> what get_view_url should hand back, using
    the same three-way vocabulary the real one uses (a URL, "" or None)."""
    monkeypatch.setattr(scrape, "get_token", lambda _s: "token")
    monkeypatch.setattr(scrape.time, "sleep", lambda _s: None)
    monkeypatch.setattr(scrape, "parse_results_page", lambda _soup: (records, 1))
    monkeypatch.setattr(scrape, "fetch_view_urls_parallel",
                        lambda rs: [view_urls[r["detail_url"]] for r in rs])

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
    records = [a_record("A", "url-a")]
    rows = drive_one_page(monkeypatch, records, {"url-a": "https://example.test/doc"})
    assert rows[0][-1] == "https://example.test/doc"


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
