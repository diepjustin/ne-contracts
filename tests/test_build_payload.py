"""The nightly build, run for real: the binary payload, read back, and what stops bad data.

Every other build test goes through build_site.main() with --emit-json, which
returns before the binary payload is written. So the path that publishes the
site -- the writers, verify_payload, the description and AI-summary blocks, the
search index, selftest.json -- ran only inside the three-minute CI build on real
data, and the guards that stop a malformed row from shipping could be refactored
away without a test noticing. These run main() on a few rows (and on a thousand,
where a ratio matters) in a scratch folder, read the result back with
ne_format.read_payload, and check that each way the data can be wrong stops the
build instead of being published.
"""

import base64
import json
import os
import shutil
import sys
import zlib
from urllib.parse import quote

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import build_site  # noqa: E402
import ne_format  # noqa: E402
import scrape  # noqa: E402

STATE_ENTITY = "Roads, Department of"
CAMPUS = sorted(scrape.HIGHER_ED_ENTITIES)[0]
MODEL = build_site.AI_MODELS[1]


# --- fixtures: tokens, URLs and rows that look like the state's --------------------

def b64(seed, size=ne_format.TOKEN_BYTES):
    """Base64 text of `size` recognisable bytes, as the state's tokens are."""
    return base64.b64encode(bytes((seed * 7 + i) % 256 for i in range(size))).decode()


def detail(dn, v=1, a="a1", d="d1", n="n1"):
    """A Detail URL as the state writes it: every value percent-encoded, in this order."""
    q = lambda s: quote(s, safe="")  # noqa: E731
    return (f"{build_site.DETAIL_BASE}?A={q(a)}&D={q(d)}&DN={q(dn)}&N={q(n)}&DT=dt1&V={q(b64(v))}")


def view(seed):
    return build_site.VIEW_BASE + quote(b64(seed), safe="")


def row(doc="1454299", *, status="Active", vendor="ACME PAVING", entity=STATE_ENTITY,
        type_="O4", amount="$4,920.00", dn=1, v=1, view_seed=1, url=None, view_url=None):
    """One CSV row. `view_seed=None` is a record the state published no document for."""
    if view_url is None:
        view_url = view(view_seed) if view_seed is not None else ""
    return [doc, type_, "027", entity, vendor, amount, "08/13/2026", "01/01/2099", status,
            url if url is not None else detail(b64(dn), v), view_url]


class Site:
    """A scratch project folder with build_site pointed at it."""

    def __init__(self, monkeypatch, root):
        self.monkeypatch, self.root = monkeypatch, root
        (root / "scripts").mkdir(parents=True)
        (root / "data").mkdir()
        shutil.copy(os.path.join(SCRIPTS, "type_groups.json"), root / "scripts" / "type_groups.json")
        self.paths = {name: str(root / "data" / (name + ".csv"))
                      for name in ("contract", "purchase-order", "state")}
        for name, value in (("ROOT", str(root)), ("OUT_DIR", str(root)), ("DATA", self.paths),
                            ("SCRAPE_META", str(root / "data" / "scrape_meta.json")),
                            ("SCOPE_JSONL", str(root / "data" / "scope.jsonl")),
                            ("AI_SUMMARIES_JSONL", str(root / "data" / "ai_summaries.jsonl")),
                            ("VENDOR_GROUPS_JSON", str(root / "scripts" / "vendor_groups.json"))):
            monkeypatch.setattr(build_site, name, value)

    def write(self, state=(), purchase_order=(), contract=()):
        import csv
        for name, rows in (("state", state), ("purchase-order", purchase_order), ("contract", contract)):
            with open(self.paths[name], "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(scrape.CSV_HEADER)
                writer.writerows(rows)

    def jsonl(self, name, records):
        with open(self.root / "data" / name, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")

    def vendor_groups(self, mapping):
        with open(self.root / "scripts" / "vendor_groups.json", "w", encoding="utf-8") as f:
            json.dump(mapping, f)

    def build(self):
        """build_site.main() with no flags; returns the folder it published into."""
        self.monkeypatch.setattr(sys, "argv", ["build_site.py"])
        build_site.main()
        with open(self.root / "manifest.json", encoding="utf-8") as f:
            return str(self.root / json.load(f)["dir"])


@pytest.fixture
def site(monkeypatch, tmp_path):
    return Site(monkeypatch, tmp_path / "project")


def described_site(site):
    """Four rows, one description, one AI summary: every block type has something to carry."""
    site.write(
        state=[row("1454299", dn=1, view_seed=1),
               row("1454300", status="Expired", vendor="BETA ROOFING", dn=2, v=2, view_seed=2),
               row("A-77", dn=3, view_seed=None)],
        purchase_order=[row("P-9", entity=CAMPUS, vendor="GAMMA SUPPLY", dn=4, v=3, view_seed=3)])
    first, second = quote(b64(1), safe=""), quote(b64(2), safe="")
    site.jsonl("scope.jsonl", [{"tok": first, "doc": "1454299", "source": "cover_sheet",
                                "description": "Resurfacing of Highway 30 near Kearney."}])
    site.jsonl("ai_summaries.jsonl", [{"tok": second, "doc": "1454300", "model": MODEL, "seconds": 1.0,
                                       "unclear": False, "summary": "A roofing contract."}])
    site.vendor_groups({"ACME": ["ACME PAVING", "BETA ROOFING"]})
    return site


def flip(path, offset=-1):
    data = bytearray(open(path, "rb").read())
    data[offset] ^= 0x01
    open(path, "wb").write(bytes(data))


def put(path, offset, value):
    data = bytearray(open(path, "rb").read())
    data[offset] = value
    open(path, "wb").write(bytes(data))


# --- a tiny full build -------------------------------------------------------------------

def test_a_tiny_full_build_writes_a_payload_that_reads_back(site, capsys):
    outdir = described_site(site).build()
    columns, docs, vendors, vtokens, meta = ne_format.read_payload(outdir)

    # Row order is the CSVs' order: contract, purchase-order, state.
    assert docs == [b"P-9", b"1454299", b"1454300", b"A-77"]
    assert vendors == [b"GAMMA SUPPLY", b"ACME PAVING", b"BETA ROOFING"]
    assert [len(t) for t in vtokens] == [ne_format.TOKEN_BYTES] * 3
    assert meta["count"] == 4 and meta["vendorCount"] == 3 and meta["blockCount"] == 1
    assert [meta["statuses"][s] for s in columns["status"]] == ["Active", "Active", "Expired", "Active"]
    assert [meta["entities"][e] for e in columns["entity"]][0] == CAMPUS
    assert list(columns["viewPresent"]) == [1, 1, 1, 0]
    assert list(columns["amount"]) == [4920.0] * 4
    assert meta["dnExceptions"] == {} and meta["tokenBytes"] == ne_format.TOKEN_BYTES
    assert "decoded payload matches the source for all 4 rows" in capsys.readouterr().out


def test_the_published_meta_carries_every_key_the_page_reads(site):
    meta = ne_format.read_payload(described_site(site).build())[4]
    wanted = {"count", "buildId", "formatVersion", "blockSize", "blockCount", "tokenBytes",
              "vendorCount", "docAlphabet", "dnExceptions", "ambiguous", "adn", "DT", "detailBase",
              "viewBase", "entities", "statuses", "types", "typeGroups", "built", "scraped",
              "incomplete", "bytes", "digests", "wordCount", "vendorGroups", "descCount",
              "descBytes", "descSources", "descSourceCounts", "aiCount", "aiBytes", "aiModels",
              "docCheckedRows", "docMultiRows", "docCountUnknown"}
    assert wanted <= set(meta), sorted(wanted - set(meta))
    assert meta["formatVersion"] == ne_format.FORMAT_VERSION
    assert meta["docAlphabet"] == "".join(sorted(set("P-9" "1454299" "1454300" "A-77")))


def test_the_manifest_names_the_build_that_was_written(site):
    outdir = described_site(site).build()
    with open(site.root / "manifest.json", encoding="utf-8") as f:
        manifest = json.load(f)
    meta = ne_format.read_payload(outdir)[4]
    assert manifest["buildId"] == meta["buildId"] and manifest["dir"] == "d/" + meta["buildId"]
    assert os.path.isdir(outdir)


def test_the_digests_and_sizes_describe_the_files_on_disk(site):
    """The page's ?selftest=1 and its progress bar trust these; a stale one lies quietly."""
    outdir = described_site(site).build()
    columns, docs, vendors, _vtokens, meta = ne_format.read_payload(outdir)

    for name, column in columns.items():
        assert meta["digests"][name] == zlib.crc32(column.tobytes()), name
    assert meta["digests"][ne_format.DOCS] == zlib.crc32(b"".join(docs))
    assert meta["digests"][ne_format.VENDORS] == zlib.crc32(
        bytes(len(v) for v in vendors) + b"".join(vendors))
    for name in (ne_format.DESC_SRC, ne_format.DESC_DOC, ne_format.AI_SRC):
        data = open(os.path.join(outdir, name), "rb").read()
        assert meta["digests"][name] == zlib.crc32(data), name
    for name, size in meta["bytes"].items():
        assert os.path.getsize(os.path.join(outdir, name)) == size, name


def test_selftest_holds_the_url_the_page_must_reproduce_for_every_row(site):
    outdir = described_site(site).build()
    fixture = json.load(open(os.path.join(outdir, ne_format.SELFTEST), encoding="utf-8"))["rows"]
    docs = ne_format.read_payload(outdir)[1]

    assert [r for r, _ in fixture] == [0, 1, 2, 3]
    expected = {b"1454299": view(1), b"1454300": view(2), b"P-9": view(3), b"A-77": detail(b64(3))}
    for index, url in fixture:
        assert url == expected[docs[index]], docs[index]


def test_descriptions_and_ai_summaries_travel_to_the_blocks_with_their_sources(site):
    outdir = described_site(site).build()
    meta = ne_format.read_payload(outdir)[4]
    docs = ne_format.read_payload(outdir)[1]
    described, summarised = docs.index(b"1454299"), docs.index(b"1454300")

    assert meta["descCount"] == 1 and meta["aiCount"] == 1
    text = ne_format.read_desc_blocks(outdir, 4)
    assert text[described] == b"Resurfacing of Highway 30 near Kearney."
    assert sum(1 for t in text if t) == 1
    codes = ne_format.read_desc_sources(outdir, 4)
    assert codes[described] == build_site.DESC_SOURCE_CODE["cover_sheet"]
    assert sum(1 for c in codes if c) == 1

    summaries = ne_format.read_ai_blocks(outdir, 4)
    assert summaries[summarised] == b"A roofing contract."
    assert sum(1 for s in summaries if s) == 1
    models = ne_format.read_ai_sources(outdir, 4)
    assert models[summarised] == build_site.AI_MODEL_CODE[MODEL]
    assert sum(1 for m in models if m) == 1
    assert meta["aiModels"] == list(build_site.AI_MODELS)

    # The one description is searchable, and nothing but descriptions is.
    assert meta["wordCount"] > 0
    assert ne_format.read_index(outdir)["highway"] == [described]


def test_a_build_with_no_descriptions_or_summaries_still_writes_every_block_it_promises(site):
    site.write(state=[row()])
    outdir = site.build()
    meta = ne_format.read_payload(outdir)[4]

    assert meta["descCount"] == 0 and meta["aiCount"] == 0 and meta["wordCount"] == 0
    assert meta["vendorGroups"] == []
    assert ne_format.read_desc_blocks(outdir, 1) == [b""]
    assert ne_format.read_ai_blocks(outdir, 1) == [b""]
    assert list(ne_format.read_ai_sources(outdir, 1)) == [0]
    assert not os.path.exists(os.path.join(outdir, ne_format.VGROUP))


def test_vendor_groups_reach_the_payload(site):
    described_site(site).vendor_groups({"_note": "ignored", "ACME": ["ACME PAVING", "BETA ROOFING"]})
    outdir = site.build()
    _columns, _docs, vendors, _vtokens, meta = ne_format.read_payload(outdir)

    assert meta["vendorGroups"] == ["ACME"]
    groups = list(ne_format.read_vendor_groups(outdir, len(vendors)))
    assert groups == [{b"ACME PAVING": 0, b"BETA ROOFING": 0}.get(v, -1) for v in vendors]


def test_the_documents_log_reaches_the_payload(site):
    described_site(site)
    url_of = {"single": detail(b64(1)), "multi": detail(b64(2), v=2)}
    site.jsonl("documents.jsonl", [
        {"k": scrape.document_key(url_of["single"]), "doc": "1454299", "entity": STATE_ENTITY,
         "n": 1, "seen": "2026-10-01"},
        {"k": scrape.document_key(url_of["multi"]), "doc": "1454300", "entity": STATE_ENTITY, "n": 2,
         "seen": "2026-10-01", "u": url_of["multi"],
         "documents": [{"name": "DOC-A", "size": "1Mb", "token": quote(b64(2), safe="")},
                       {"name": "DOC-B", "size": "9Kb", "token": quote(b64(20), safe="")}]}])
    outdir = site.build()
    columns, docs, _v, _t, meta = ne_format.read_payload(outdir)
    single, multi, unasked = docs.index(b"1454299"), docs.index(b"1454300"), docs.index(b"A-77")

    assert columns["docCount"][single] == 1 and columns["docCount"][multi] == 2
    assert columns["docCount"][unasked] == ne_format.DOC_COUNT_UNKNOWN
    assert meta["docCheckedRows"] == 2 and meta["docMultiRows"] == 1
    listed = ne_format.read_xdoc_blocks(outdir, 4)
    assert [d["name"] for d in ne_format.unpack_documents(listed[multi])] == ["DOC-A", "DOC-B"]
    assert not listed[single]


# --- rows the build must not publish -----------------------------------------------------

GUARDS = [
    ("an entity the scraper does not know", lambda: [row(entity="Department of Imaginary Things")],
     "unexpected entity"),
    ("a view URL on another host", lambda: [row(view_url="https://example.invalid/ViewDocument?D=abc")],
     "unexpected view URL shape"),
    ("a document number over 255 bytes", lambda: [row("9" * 256)], "exceeds 255 bytes"),
    ("a lowercase document number", lambda: [row("ab-123")], "contains lowercase"),
    ("one vendor under two V tokens", lambda: [row("1", dn=1, v=1), row("2", dn=2, v=2)],
     "maps to two V tokens"),
    ("a view token of the wrong length", lambda: [row(view_url=view_with_bytes(15))],
     "decodes to 15 bytes"),
    ("a DN token of the wrong length", lambda: [row(url=detail(b64(4, 12)))],
     "DN tokens are not 16 bytes"),
]


def view_with_bytes(size):
    return build_site.VIEW_BASE + quote(b64(5, size), safe="")


@pytest.mark.parametrize("label, rows, message", GUARDS, ids=[g[0] for g in GUARDS])
def test_the_build_refuses_data_it_cannot_represent(site, label, rows, message):
    site.write(state=rows())
    with pytest.raises(SystemExit, match=message):
        site.build()
    assert not os.path.exists(site.root / "manifest.json"), "a refused build must not name a payload"


def test_a_good_row_beside_a_bad_one_does_not_get_published_alone(site):
    site.write(state=[row("1454299"), row("ab-123", dn=2)])
    with pytest.raises(SystemExit):
        site.build()
    assert not os.path.exists(site.root / "d")


# --- the build's own dictionaries and ceilings, at their edges ---------------------------------

def thousand(odd=0, size=24):
    """1,000 distinct rows, the first `odd` of them with a DN token of `size` bytes.

    A longer token is the working case: write_token_blocks keeps the first 16
    bytes and the whole token travels in meta.dnExceptions. A shorter one is not:
    it is written unpadded, so the block comes up short and verify_payload stops
    the build with a bare ValueError from read_token_blocks (fail-closed;
    build-04 item c). It is deliberately not pinned here, because the reviewers
    disagreed on whether the fix is to pad it or to refuse it with a message.
    """
    rows = []
    for i in range(1000):
        token_size = size if i < odd else ne_format.TOKEN_BYTES
        rows.append(row(str(1000000 + i), url=detail(b64(i % 200 + 10, token_size)), view_seed=None))
    return rows


def test_a_thousandth_of_the_rows_may_have_an_odd_dn_token_and_it_travels_in_meta(site):
    site.write(state=thousand(odd=1))
    columns, docs, _v, _t, meta = ne_format.read_payload(site.build())
    assert meta["count"] == 1000 and list(meta["dnExceptions"]) == ["0"]
    assert meta["dnExceptions"]["0"] == b64(10, 24)
    assert docs[0] == b"1000000"


def test_more_than_a_thousandth_stops_the_build(site):
    site.write(state=thousand(odd=2))
    with pytest.raises(SystemExit, match="2 DN tokens are not 16 bytes"):
        site.build()


@pytest.mark.xfail(strict=True, reason=(
    "build-04: a row with no Detail URL has an empty DN, which goes into meta.dnExceptions as '' "
    "and is then read back with `exceptions.get(str(i)) or ...`, where '' is falsy. The build "
    "dies in verify_payload with 'DN token round trip failed'. Nothing in the data has one today. "
    "The fix tests membership (build_site.py verify_payload, and index.html's matching `||`); "
    "when it lands this passes, strict=True fails it, and the marker should go."))
def test_a_row_with_no_detail_url_builds_and_verifies(site):
    site.write(state=thousand() + [row("DL-1", url="", view_seed=None)])
    assert ne_format.read_payload(site.build())[4]["count"] == 1001


def distinct_adn(count):
    return [row(str(2000000 + i), url=detail(b64(i % 200 + 10), n=f"n{i}"), view_seed=None)
            for i in range(count)]


def distinct_types(count):
    return [row(str(3000000 + i), type_=f"T{i}", dn=i % 200 + 10, view_seed=None)
            for i in range(count)]


def test_255_address_triples_fit_a_u8_column(site):
    site.write(state=distinct_adn(255))
    assert len(ne_format.read_payload(site.build())[4]["adn"]) == 255


def test_255_document_type_codes_fit_a_u8_column(site):
    site.write(state=distinct_types(255))
    assert len(ne_format.read_payload(site.build())[4]["types"]) == 255


@pytest.mark.xfail(strict=True, raises=OverflowError, reason=(
    "build-04: the u8 dictionary-size guard runs after the rows are appended, so one entry past "
    "256 raises OverflowError from array('B') before the friendly message is reachable. Moving the "
    "check ahead of the appends makes this a clean SystemExit; the marker should then go."))
@pytest.mark.parametrize("make_rows", [distinct_adn, distinct_types], ids=["address triples", "type codes"])
def test_more_entries_than_a_u8_column_holds_stop_the_build_cleanly(site, make_rows):
    site.write(state=make_rows(257))
    with pytest.raises(SystemExit):
        site.build()


@pytest.mark.xfail(strict=True, reason=(
    "build-04: to_amount('NaN') is nan, nan != nan, so the byte comparison in verify_payload "
    "reports 'rows 0..N differ after decoding' for a whole 100,000-row chunk. Not in the data "
    "today. Returning 0.0 for a non-finite amount (math.isfinite) is the fix."))
@pytest.mark.parametrize("raw", ["NaN", "inf", "-Infinity"])
def test_a_non_finite_amount_is_read_as_zero(raw):
    assert build_site.to_amount(raw) == 0.0


# --- which rows count as one ----------------------------------------------------------------

def test_two_active_rows_that_differ_only_in_detail_url_are_both_published(site):
    """A real shape: Peru State College 80-3-3339 and UNMC E000734430 each have a
    pair that share document number, entity, status and type, and differ in the
    V token of their Detail URL. Dropping the URL from the duplicate check would
    quietly merge the second away."""
    site.write(state=[row("80-3-3339", dn=1, v=1, view_seed=None),
                      row("80-3-3339", dn=1, v=2, view_seed=None, vendor="OTHER VENDOR")])
    assert ne_format.read_payload(site.build())[4]["count"] == 2


def test_a_row_a_resumed_scrape_wrote_twice_is_published_once(site, capsys):
    site.write(state=[row(), row()])
    assert ne_format.read_payload(site.build())[4]["count"] == 1
    assert "duplicate rows: 1 dropped" in capsys.readouterr().out


def test_a_nan_amount_is_never_published(site):
    """NaN != NaN, so the rows rebuilt from the decoded files never equal the reference rows and
    verify_payload stops the build. Either that, or (build-04's fix) the amount is read as 0.0;
    what must not happen is a NaN in the payload."""
    site.write(state=[row(amount="NaN"), row("1454300", dn=2)])
    try:
        outdir = site.build()
    except SystemExit as stop:
        assert "differ after decoding" in str(stop)
        return
    amounts = ne_format.read_payload(outdir)[0]["amount"]
    assert all(a == a for a in amounts)


# --- a payload that does not read back is not published ----------------------------------------

CORRUPTIONS = [
    ("an amount", "write_payload", lambda out: flip(os.path.join(out, ne_format.COLS_F64)),
     "column 'amount' does not survive the round trip"),
    ("a document number", "write_payload", lambda out: flip(os.path.join(out, ne_format.DOCS)),
     "document numbers do not survive the round trip"),
    ("a vendor name", "write_payload", lambda out: flip(os.path.join(out, ne_format.VENDORS)),
     "vendor names do not survive the round trip"),
    ("a vendor token", "write_payload", lambda out: flip(os.path.join(out, ne_format.VTOK)),
     "vendor tokens do not survive the round trip"),
    ("a DN token", "write_token_blocks", lambda out: flip(ne_format.block_path(out, 0), 0),
     "row 0: DN token round trip failed"),
    ("a view token", "write_token_blocks", lambda out: flip(ne_format.block_path(out, 0), 4 * ne_format.TOKEN_BYTES),
     "row 0: view token round trip failed"),
    ("a description", "write_desc_blocks", lambda out: flip(ne_format.desc_path(out, 0)),
     "description round trip failed"),
    ("an AI summary", "write_ai_blocks", lambda out: flip(ne_format.ai_path(out, 0)),
     "AI summary round trip failed"),
    ("the search index", "write_index", lambda out: flip(os.path.join(out, ne_format.POSTINGS)),
     "search index does not survive the round trip"),
    ("a vendor group", "write_vendor_groups", lambda out: flip(os.path.join(out, ne_format.VGROUP), 0),
     "vendor groups do not survive the round trip"),
    # described_site: the description is on row 1, the AI summary on row 2, nothing on row 0.
    ("a description source on a row with no description", "write_desc_sources",
     lambda out: put(os.path.join(out, ne_format.DESC_SRC), 0, 1), "description text and source disagree"),
    ("a description with no source", "write_desc_sources",
     lambda out: put(os.path.join(out, ne_format.DESC_SRC), 1, 0), "description text and source disagree"),
    ("an AI model on a row with no summary", "write_ai_sources",
     lambda out: put(os.path.join(out, ne_format.AI_SRC), 0, 1), "AI summary text and source disagree"),
    ("an AI summary with no model", "write_ai_sources",
     lambda out: put(os.path.join(out, ne_format.AI_SRC), 2, 0), "AI summary text and source disagree"),
]


@pytest.mark.parametrize("label, writer, damage, message", CORRUPTIONS, ids=[c[0] for c in CORRUPTIONS])
def test_a_payload_that_does_not_read_back_stops_the_build(site, monkeypatch, label, writer, damage, message):
    described_site(site)
    original = getattr(ne_format, writer)

    def writes_then_damages(outdir, *args, **kwargs):
        result = original(outdir, *args, **kwargs)
        damage(outdir)
        return result

    monkeypatch.setattr(ne_format, writer, writes_then_damages)
    with pytest.raises(SystemExit, match=message):
        site.build()


def test_a_meta_key_lost_after_the_first_write_stops_the_build(site, monkeypatch):
    """write_payload works on a copy, so a key added afterwards reaches disk only if
    meta.json is written again. This is how every full build once shipped with no
    vendorGroups and no wordCount."""
    described_site(site)
    original = ne_format.write_meta

    def forgets_vendor_groups(outdir, meta):
        original(outdir, {k: v for k, v in meta.items() if k != "vendorGroups"})

    monkeypatch.setattr(ne_format, "write_meta", forgets_vendor_groups)
    with pytest.raises(SystemExit, match="meta.json is missing 'vendorGroups'"):
        site.build()
