"""extract_text.extract: what a document is called decides whether it is ever asked about again.

Four outcomes matter. 'unavailable' (a 404) is never retried; 'unsupported' (a file
we cannot parse) is not either, but it is not the same fact; 'error' is retried; and
'scanned' feeds OCR, so a PDF wrongly called 'text' is silently never OCR'd and one
wrongly called 'scanned' is OCR'd for nothing. On 22 Aug 2026 every document request
returned an HTML error page with status 200, and filing that as 'unavailable' would
have recorded 14,586 present documents as absent. None of this had a test: extract()
was never called by one. Real PDFs are built here -- one by hand, one by pypdf -- and
nothing touches the network or data/.
"""

import hashlib
import os
import sys
from io import BytesIO

import pytest
import requests
from pypdf import PdfWriter

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import extract_text  # noqa: E402

URL = "https://example.test/Search/ViewDocument?D=abc"
SENTENCE = "This agreement is made between the State of Nebraska and the Contractor named herein. "


# --- documents ------------------------------------------------------------------------------

def text_pdf(page_texts):
    """A minimal valid PDF with one Helvetica line per page, offsets and all."""
    objects = {3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids, next_id = [], 4
    for text in page_texts:
        content_id, page_id = next_id, next_id + 1
        next_id += 2
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = ("BT /F1 10 Tf 20 700 Td (%s) Tj ET" % escaped).encode("latin-1")
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objects[page_id] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 2000 792] /Contents %d 0 R "
                            b"/Resources << /Font << /F1 3 0 R >> >> >>" % content_id)
        kids.append(b"%d 0 R" % page_id)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % len(kids)

    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for number in sorted(objects):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def blank_pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(612, 792)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class Resp:
    def __init__(self, status=200, content=b"", content_type=None):
        self.status_code, self.content = status, content
        self.headers = {"Content-Type": content_type} if content_type else {}

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")


class Session:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.timeouts = response, error, []

    def get(self, url, timeout=None):
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        return self.response


def extract(response=None, error=None, **kwargs):
    return extract_text.extract(Session(response, error), URL, **kwargs)


# --- the PDF cases that need no stubs ---------------------------------------------------------

def test_a_pdf_with_a_text_layer_is_text_and_carries_its_words():
    body = text_pdf([SENTENCE * 2, SENTENCE * 2, SENTENCE * 2])
    result = extract(Resp(content=body))

    assert result["status"] == "text" and result["pages"] == 3
    assert "State of Nebraska" in result["text"] and result["chars"] >= 3 * 100
    assert "failed_pages" not in result and "parsed_pages" not in result


def test_a_pdf_with_blank_pages_is_scanned_and_says_how_long_it_is():
    assert extract(Resp(content=blank_pdf(3))) == {"status": "scanned", "pages": 3, "chars": 0}


def test_a_pdf_with_almost_no_text_is_scanned_not_text():
    """A page number and a stamp on a scan: real text, but nothing a parser can use."""
    result = extract(Resp(content=text_pdf(["Page 1", "Page 2"])))
    assert result["status"] == "scanned" and result["pages"] == 2 and "text" not in result


def test_the_pdf_magic_number_decides_not_the_content_type():
    pdf = text_pdf([SENTENCE * 2])
    assert extract(Resp(content=pdf, content_type="text/html"))["status"] == "text"
    assert extract(Resp(content=b"<html>" + pdf, content_type="application/pdf"))["status"] == "error"


def test_the_request_is_made_with_a_timeout():
    session = Session(Resp(content=blank_pdf()))
    extract_text.extract(session, URL)
    assert session.timeouts == [60]


# --- the threshold, and how pages are read -------------------------------------------------------

class FakePage:
    def __init__(self, text="", fails=False):
        self.text, self.fails = text, fails

    def extract_text(self):
        if self.fails:
            raise ValueError("unresolved IndirectObject in /Widths")
        return self.text


@pytest.fixture
def pages(monkeypatch):
    """Make PdfReader read a scripted list of pages from any body that starts %PDF."""
    def use(*scripted):
        class Reader:
            def __init__(self, stream):
                self.pages = list(scripted)
        monkeypatch.setattr(extract_text, "PdfReader", Reader)
        return Resp(content=b"%PDF-1.4 stub")
    return use


def test_the_scanned_threshold_is_fifty_characters_a_page_inclusive(pages):
    assert extract_text.MIN_CHARS_PER_PAGE == 50
    assert extract(pages(FakePage("x" * 50), FakePage("x" * 50)))["status"] == "text"
    assert extract(pages(FakePage("x" * 49), FakePage("x" * 49)))["status"] == "scanned"


def test_the_average_is_over_the_pages_read_not_the_whole_document(pages):
    """Two pages of 100 characters, read before the storage limit, in a ten-page document."""
    result = extract(pages(*[FakePage("y" * 100)] * 10), store_chars=150)
    assert result["status"] == "text" and result["pages"] == 10
    assert result["parsed_pages"] == 2 and result["chars"] == 200


def test_reading_stops_once_enough_text_is_kept_and_not_before(pages):
    result = extract(pages(*[FakePage("z" * 100)] * 5), store_chars=200)
    assert result["parsed_pages"] == 2           # 200 >= 200 stops it
    result = extract(pages(*[FakePage("z" * 100)] * 5), store_chars=201)
    assert result["parsed_pages"] == 3
    assert "parsed_pages" not in extract(pages(*[FakePage("z" * 100)] * 5), store_chars=0)


def test_a_scan_is_never_cut_short_by_the_storage_limit(pages):
    result = extract(pages(*[FakePage("")] * 6), store_chars=10)
    assert result == {"status": "scanned", "pages": 6, "chars": 0}


def test_one_page_that_raises_costs_that_page_not_the_document(pages):
    result = extract(pages(FakePage("a" * 100), FakePage(fails=True), FakePage("b" * 100)))
    assert result["status"] == "text" and result["failed_pages"] == 1
    assert result["pages"] == 3 and result["chars"] == 200


def test_every_page_raising_is_a_parse_failure_not_a_scan(pages, monkeypatch):
    seen = []

    def second_opinion(content, why, store_chars):
        seen.append(str(why))
        return {"status": "error", "detail": "asked pdfminer"}

    monkeypatch.setattr(extract_text, "with_pdfminer", second_opinion)
    result = extract(pages(FakePage(fails=True), FakePage(fails=True)))
    assert result["detail"] == "asked pdfminer" and "all 2 pages" in seen[0]


# --- the second opinion ------------------------------------------------------------------------------

def test_a_pdf_pypdf_cannot_open_gets_a_second_opinion_from_pdfminer(monkeypatch):
    def refuses(stream):
        raise ValueError("Invalid object in /Pages")

    monkeypatch.setattr(extract_text, "PdfReader", refuses)
    monkeypatch.setattr(extract_text, "pdfminer_text", lambda stream: ("w" * 120 + "\f") * 3)
    result = extract(Resp(content=b"%PDF-1.4 stub"))
    assert result == {"status": "text", "pages": 3, "chars": 363, "text": ("w" * 120 + "\f") * 3,
                      "parser": "pdfminer"}


def test_pdfminer_reading_nothing_is_scanned_and_one_page_without_a_form_feed(monkeypatch):
    monkeypatch.setattr(extract_text, "pdfminer_text", lambda stream: "")
    assert extract_text.with_pdfminer(b"%PDF", ValueError("x")) == {
        "status": "scanned", "pages": 1, "chars": 0, "parser": "pdfminer"}


@pytest.mark.parametrize("per_page, status", [(50, "text"), (49, "scanned")])
def test_the_second_opinion_uses_the_same_threshold_per_form_feed_page(monkeypatch, per_page, status):
    """Each page's text ends in a form feed, which counts as a character."""
    text = ("q" * (per_page - 1) + "\f") * 2
    monkeypatch.setattr(extract_text, "pdfminer_text", lambda stream: text)
    assert extract_text.with_pdfminer(b"%PDF", ValueError("x"))["status"] == status


def test_a_pdf_neither_parser_can_open_is_an_error_that_names_both(monkeypatch):
    def fails(stream):
        raise ValueError("boom")

    monkeypatch.setattr(extract_text, "pdfminer_text", fails)
    result = extract(Resp(content=b"%PDF-1.4 this is not a pdf"))
    assert result["status"] == "error" and result["detail"].startswith("pypdf: ")
    assert "| pdfminer: boom" in result["detail"]


# --- everything that is not a readable PDF ----------------------------------------------------------------

def test_a_404_is_unavailable_and_nothing_else_is():
    assert extract(Resp(404)) == {"status": "unavailable", "detail": "HTTP 404"}
    for status in (403, 410, 500, 503):
        assert extract(Resp(status))["status"] == "error", status


def test_an_error_status_is_never_read_as_a_document_even_if_a_pdf_came_with_it():
    for status in (403, 500):
        result = extract(Resp(status, content=text_pdf([SENTENCE * 2])))
        assert result["status"] == "error" and f"{status} Server Error" in result["detail"], status


def test_a_failed_request_is_an_error_whose_detail_is_kept_short():
    result = extract(error=requests.ConnectionError("x" * 500))
    assert result["status"] == "error" and len(result["detail"]) == 200


def test_the_state_serving_an_error_page_with_status_200_is_an_error_not_an_absence():
    """22 Aug 2026: 'An internal error occured: An error occurred within the Unity API'."""
    body = ("<html><body>\n  An internal error occured: An error occurred within the Unity API:\n"
            "  The type initializer threw an exception.</body></html>").encode()
    result = extract(Resp(content=body, content_type="text/html; charset=utf-8"))

    assert result["status"] == "error"
    assert result["contentType"] == "text/html; charset=utf-8"
    assert result["bodySha256"] == hashlib.sha256(body).hexdigest()
    assert result["bodyHead"].startswith("<html><body> An internal error occured")
    assert "\n" not in result["bodyHead"] and len(result["bodyHead"]) <= extract_text.BODY_HEAD_CHARS


def test_the_opening_text_of_an_error_page_is_capped_and_whitespace_collapsed():
    body = ("<html>" + "   very   long   outage   notice   " * 100 + "</html>").encode()
    result = extract(Resp(content=body, content_type="text/html"))
    assert len(result["bodyHead"]) == extract_text.BODY_HEAD_CHARS == 300
    assert "  " not in result["bodyHead"]


@pytest.mark.parametrize("content_type", ["image/tiff", "image/jpeg", "application/msword",
                                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                          "application/rtf", "IMAGE/TIFF"])
def test_a_file_we_cannot_parse_is_unsupported_and_not_retried(content_type):
    result = extract(Resp(content=b"II*\x00binary", content_type=content_type))
    assert result["status"] == "unsupported" and "bodyHead" not in result
    assert result["contentType"] == content_type.lower()


def test_a_response_with_no_content_type_is_an_error_to_try_again():
    result = extract(Resp(content=b"???"))
    assert result["status"] == "error" and result["contentType"] == "?"
