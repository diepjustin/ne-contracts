"""Builder helpers, and the two guards that protect readers from silence.

The bugs worth preventing here do not raise. `incomplete_coverage()` returning
everything looks like a cautious warning rather than a fault. A permalink
collision opens the wrong contract without erroring. A description carried onto
the wrong row shows one contract's words beside another's money. None of these
announce themselves, which is exactly why they need tests.
"""

import array
import base64
import json
import os
import re
import sys
from urllib.parse import quote, unquote

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import build_site  # noqa: E402
import ne_format  # noqa: E402


# --- parsing the state's own formatting ------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("01/15/2011", 20110115),
    ("9/3/2024", 20240903),
    ("09/28/2223", 22230928),   # the state's own typo year, preserved not fixed
    ("", 0),
    ("not a date", 0),
    ("1/2", 0),                 # too few parts
    ("13/45/2020", 20130000 + 4520 - 4520 + 4500),  # nonsense passes through as digits
])
def test_to_ymd(raw, expected):
    if raw == "13/45/2020":
        # No calendar validation on purpose: the job is to reproduce what the
        # state published, and an invalid date is a finding, not a parse error.
        assert build_site.to_ymd(raw) == 20201345
    else:
        assert build_site.to_ymd(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("$1,234.56", 1234.56),
    ("1234.56", 1234.56),
    ("$38,025,000,000.00", 38025000000.0),
    ("  $0.00 ", 0.0),
    ("", 0.0),
    ("N/A", 0.0),
])
def test_to_amount(raw, expected):
    assert build_site.to_amount(raw) == expected


# --- permalinks -------------------------------------------------------------

def test_unique_triples_have_no_collision():
    docs = [b"100", b"100", b"200"]
    entity = [0, 1, 0]        # same document number, different agency -- legitimate
    type_ = [5, 5, 5]
    assert build_site.find_permalink_collision(docs, entity, type_, ["a", "b", "c"]) == []


def test_a_repeated_triple_pointing_at_two_documents_is_a_collision():
    """The case worth blocking: `45500` at the Medical Center is $2,558,983
    expired and $21,204,743 active, two different PDFs under one permalink."""
    docs = [b"100", b"200", b"100"]
    entity = [0, 1, 0]
    type_ = [5, 5, 5]
    groups = build_site.find_permalink_collision(docs, entity, type_, ["x", "y", "z"])
    assert groups == [[0, 2]]


def test_a_repeated_triple_for_one_document_is_not_a_collision():
    """171 of the 176 repeats are the state carrying one contract under two
    vendor spellings -- same amount, same dates, same PDF. A link resolving to
    either row lands in the right place, so blocking the publish over it stops
    738,195 records from updating for no reader benefit. This is what the
    first version of the guard got wrong, and it took the nightly build down
    on 17 Aug 2026 while the five real cases went unreported."""
    docs = [b"CW32981", b"CW32981"]
    assert build_site.find_permalink_collision(docs, [0, 0], [5, 5], ["same", "same"]) == []


def test_same_document_number_across_types_is_not_a_collision():
    """14,633 document numbers are reused; type is part of what separates them."""
    assert build_site.find_permalink_collision([b"A", b"A"], [0, 0], [1, 2], ["p", "q"]) == []


def test_every_row_of_an_ambiguous_group_is_reported():
    """The page needs a disambiguator on all of them, not just the duplicate:
    reporting only the second row would leave the first still resolving to
    whichever the scan happened to reach first."""
    docs = [b"9", b"9", b"9"]
    groups = build_site.find_permalink_collision(docs, [0, 0, 0], [1, 1, 1], ["a", "b", "a"])
    assert groups == [[0, 1, 2]]


# --- the coverage note ------------------------------------------------------

def test_incomplete_coverage_unpacks_the_progress_pair(monkeypatch, tmp_path):
    """load_progress returns (finished combos, partial positions).

    Binding that pair to a single name makes every membership test false, so
    every entity reads as uncollected and the page announces the whole state as
    "still being collected" -- the note that exists to stop readers misreading
    a gap, inverted into a false alarm across all 101 entities. It shipped, and
    went unnoticed because a too-cautious warning looks responsible.
    """
    import scrape

    monkeypatch.setattr(scrape, "HIGHER_ED_ENTITIES", ["Campus A"], raising=False)
    monkeypatch.setattr(scrape, "STATE_ENTITIES", ["Agency B"], raising=False)
    monkeypatch.setattr(scrape, "STATUSES", ["Active", "Expired"], raising=False)
    monkeypatch.setattr(
        scrape, "load_progress",
        lambda path: ({("Campus A", "Active"), ("Campus A", "Expired"),
                       ("Agency B", "Active"), ("Agency B", "Expired")}, {}),
        raising=False)

    assert build_site.incomplete_coverage() == [], \
        "everything is collected, so the page must claim no gaps"


def test_incomplete_coverage_reports_a_real_gap(monkeypatch):
    import scrape

    monkeypatch.setattr(scrape, "HIGHER_ED_ENTITIES", ["Campus A"], raising=False)
    monkeypatch.setattr(scrape, "STATE_ENTITIES", ["Agency B"], raising=False)
    monkeypatch.setattr(scrape, "STATUSES", ["Active", "Expired"], raising=False)
    # Campus A never finished its Expired half.
    monkeypatch.setattr(
        scrape, "load_progress",
        lambda path: ({("Campus A", "Active"), ("Agency B", "Active"),
                       ("Agency B", "Expired")}, {}),
        raising=False)

    gaps = build_site.incomplete_coverage()
    assert any("Campus A" in g for g in gaps)
    assert not any("Agency B" in g for g in gaps)


# --- the search index -------------------------------------------------------

def test_index_tokenises_on_punctuation_and_keeps_numbers():
    postings = build_site.build_index({0: b"1/4 in. Square Head", 1: b"CP3306-665"})
    # Punctuation separates rather than counts, so either half of a hyphenated
    # part number finds the row. Note what is absent: "1" and "4" are single
    # characters and are dropped, so a fractional size is not searchable on its
    # digits -- a deliberate trade, since one-character tokens match most of the
    # corpus and discriminate nothing. "square head" still finds it.
    assert set(postings) == {"in", "square", "head", "cp3306", "665"}
    assert postings["square"] == [0]
    assert postings["cp3306"] == [1] and postings["665"] == [1]


def test_index_drops_single_characters_but_keeps_short_numbers():
    postings = build_site.build_index({0: b"a bb 12 x"})
    assert "a" not in postings and "x" not in postings
    assert postings["bb"] == [0] and postings["12"] == [0]


def test_a_word_repeated_in_one_description_lists_its_row_once():
    postings = build_site.build_index({4: b"pump pump PUMP"})
    assert postings["pump"] == [4]


def test_postings_are_sorted_so_deltas_stay_positive():
    postings = build_site.build_index({9: b"steel", 2: b"steel", 40: b"steel"})
    assert postings["steel"] == [2, 9, 40]


def test_a_missing_checkpoint_claims_no_gaps_rather_than_every_gap(monkeypatch, tmp_path):
    """Moving the build into CI left the scrapers' checkpoints on the laptop,
    and the live site spent 17 Aug 2026 telling readers every one of the 101
    entities was "still being collected" while collection was finished.

    Absent a checkpoint the honest answer is that coverage is unknown. Of the
    three ways to be wrong, announcing 101 false gaps is the loudest and the
    worst: it discredits every count on the page at once.
    """
    import scrape

    monkeypatch.setattr(scrape, "HIGHER_ED_ENTITIES", ["Campus A"], raising=False)
    monkeypatch.setattr(scrape, "STATE_ENTITIES", ["Agency B"], raising=False)
    monkeypatch.setattr(scrape, "STATUSES", ["Active", "Expired"], raising=False)
    monkeypatch.setattr(scrape, "progress_file",
                        lambda dataset: f"data/{dataset}_definitely_not_here.json",
                        raising=False)
    # Would report every entity as missing if the absent file were read as
    # "nothing finished".
    monkeypatch.setattr(scrape, "load_progress", lambda path: (set(), {}), raising=False)

    assert build_site.incomplete_coverage() == []


def test_a_row_with_no_document_still_collides_with_its_renewal():
    """Peru State College's 80-3-3305: Tutor.com at $5,600 twice, the 2025-26
    term expired with no document and the 2026-27 renewal active with one.
    Two records, one triple, so the page needs to tell them apart."""
    groups = build_site.find_permalink_collision(
        [b"80-3-3305", b"80-3-3305"], [0, 0], [1, 1], ["", "va6hye3GoNx9"])
    assert groups == [[0, 1]]


def test_one_document_less_row_per_group_is_addressable_by_the_bare_permalink():
    """It has no view token to be named by, so it is named by not having one.
    The page resolves a permalink with no &d= to the member with no document,
    which works precisely while there is one such member."""
    groups = build_site.find_permalink_collision(
        [b"A", b"A"], [0, 0], [1, 1], ["", "token"])
    missing = [[r for r in g if not ["", "token"][r]] for g in groups]
    assert missing == [[0]]


def test_two_document_less_rows_in_one_group_cannot_both_be_the_bare_permalink():
    """The case that must still refuse. Both rows would answer to the same
    link, and nothing in the payload could say which was meant. Measured over
    741,653 rows this has never occurred -- 302 ambiguous groups, one with a
    document-less member, none with two."""
    tokens = ["", "", "token"]
    groups = build_site.find_permalink_collision(
        [b"A", b"A", b"A"], [0, 0, 0], [1, 1, 1], tokens)
    assert groups == [[0, 1, 2]]
    missing = [r for r in groups[0] if not tokens[r]]
    assert len(missing) == 2      # build_site.main() exits on exactly this


# --- which document a description is read from ------------------------------
#
# Until Aug 2026 a row's description could only come from its first document,
# because that was the only one the scraper had ever captured. Now a record
# publishing several can be described by a later one -- but only where the
# first says nothing. An amendment's words are about the amendment, and
# swapping them in over a contract's own description would quietly change what
# the page says a record is for.

def _scope(tmp_path, records):
    path = tmp_path / "scope.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return str(path)


def _documents(tmp_path, entries):
    path = tmp_path / "documents.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return str(path)


def _run(monkeypatch, tmp_path, view_tokens, docs_entries, scope_records):
    import build_site
    monkeypatch.setattr(build_site, "ROOT", str(tmp_path))
    monkeypatch.setattr(build_site, "SCOPE_JSONL", _scope(tmp_path, scope_records))
    (tmp_path / "data").mkdir(exist_ok=True)
    os.replace(_documents(tmp_path, docs_entries),
               str(tmp_path / "data" / "documents.jsonl"))
    return build_site.load_descriptions(view_tokens, carry=False)


def test_the_first_document_still_wins_when_it_says_anything(monkeypatch, tmp_path):
    """Nothing already published is displaced by an amendment."""
    entry = {"k": "k1", "doc": "D1", "entity": "E", "n": 2,
             "documents": [{"name": "A", "size": "1Mb", "token": "primary"},
                           {"name": "B", "size": "1Mb", "token": "extra"}]}
    descriptions, _sources, documents = _run(
        monkeypatch, tmp_path, ["primary"], [entry],
        [{"tok": "extra", "doc": "D1", "source": "cover_sheet", "description": "AMENDMENT"},
         {"tok": "primary", "doc": "D1", "source": "cover_sheet", "description": "CONTRACT"}])
    assert descriptions[0] == b"CONTRACT"
    assert documents[0] == 0


def test_a_later_document_fills_a_blank_and_says_which(monkeypatch, tmp_path):
    """The whole point: 22,342 rows show nothing while a document behind them
    does. The position recorded is the state's own ordering, so the panel marks
    the document the words actually came from."""
    entry = {"k": "k1", "doc": "D1", "entity": "E", "n": 3,
             "documents": [{"name": "A", "size": "1Mb", "token": "primary"},
                           {"name": "B", "size": "1Mb", "token": "second"},
                           {"name": "C", "size": "1Mb", "token": "third"}]}
    descriptions, _sources, documents = _run(
        monkeypatch, tmp_path, ["primary"], [entry],
        [{"tok": "third", "doc": "D1", "source": "cover_sheet", "description": "FROM THIRD"}])
    assert descriptions[0] == b"FROM THIRD"
    assert documents[0] == 2


def test_the_earliest_document_wins_among_several_blanks(monkeypatch, tmp_path):
    """Deterministic, and it is the state's order rather than ours."""
    entry = {"k": "k1", "doc": "D1", "entity": "E", "n": 3,
             "documents": [{"name": "A", "size": "1Mb", "token": "primary"},
                           {"name": "B", "size": "1Mb", "token": "second"},
                           {"name": "C", "size": "1Mb", "token": "third"}]}
    descriptions, _sources, documents = _run(
        monkeypatch, tmp_path, ["primary"], [entry],
        [{"tok": "third", "doc": "D1", "source": "cover_sheet", "description": "THIRD"},
         {"tok": "second", "doc": "D1", "source": "cover_sheet", "description": "SECOND"}])
    assert descriptions[0] == b"SECOND"
    assert documents[0] == 1


def test_a_row_whose_primary_has_drifted_is_still_matched(monkeypatch, tmp_path):
    """The CSV points at a document the state no longer lists first. That row's
    own document still identifies it, and still outranks the newer arrival --
    it is the one its existing description was read from."""
    entry = {"k": "k1", "doc": "D1", "entity": "E", "n": 2,
             "documents": [{"name": "NEW", "size": "1Mb", "token": "newcomer"},
                           {"name": "OLD", "size": "1Mb", "token": "csv-primary"}]}
    descriptions, _sources, documents = _run(
        monkeypatch, tmp_path, ["csv-primary"], [entry],
        [{"tok": "newcomer", "doc": "D1", "source": "cover_sheet", "description": "NEW"},
         {"tok": "csv-primary", "doc": "D1", "source": "cover_sheet", "description": "OLD"}])
    assert descriptions[0] == b"OLD"
    assert documents[0] == 1          # its position in the state's list today


def test_a_row_with_one_document_still_gets_its_description(monkeypatch, tmp_path):
    """The case that took 532,720 descriptions off the live site.

    documents.jsonl stores a document list only for records publishing more
    than one, so the ~95% publishing a single document appear in it with no
    list at all. A token map built from that file alone therefore knows almost
    nothing, and every ordinary row's description stops matching.

    It survived local builds because carry_descriptions_forward supplied those
    descriptions from the previous payload, so the totals looked right while
    the join beneath them answered for 18,305 rows out of 543,000. carry=False
    here is what CI does on a fresh build, and it is the only way to see it."""
    entry = {"k": "k1", "doc": "D1", "entity": "E", "n": 1}      # no document list
    descriptions, _sources, documents = _run(
        monkeypatch, tmp_path, ["only-doc"], [entry],
        [{"tok": "only-doc", "doc": "D1", "source": "line_items",
          "description": "ORDINARY ROW"}])
    assert descriptions[0] == b"ORDINARY ROW"
    assert documents[0] == 0


def test_a_row_absent_from_the_documents_log_still_matches(monkeypatch, tmp_path):
    """A record scraped after the last backfill is in no document log at all.
    Its description must still join, on the View URL the CSV already holds."""
    descriptions, _sources, _documents = _run(
        monkeypatch, tmp_path, ["never-backfilled"], [],
        [{"tok": "never-backfilled", "doc": "D9", "source": "line_items",
          "description": "SCRAPED LAST NIGHT"}])
    assert descriptions[0] == b"SCRAPED LAST NIGHT"


# --- carrying a description into the next build -----------------------------
#
# pages.yml restores the previous payload before every nightly dispatch, so
# carry_descriptions_forward runs on every nightly build, not just locally. Every
# test above passes carry=False, which is how it went untested while it
# relabelled every carried row "unknown", placed it on the record's first
# document and ranked it as though read from there -- and the next night's
# scope.jsonl record of the document it really came from was then outranked by
# the carried copy of itself. The live build of 28 Sep 2026 had 1,090 such
# rows, 1,050 of them read from a later document, the page saying
# "description read from this one" beside the first.

COVER_SHEET = build_site.DESC_SOURCE_CODE["cover_sheet"]
UNKNOWN = build_site.DESC_SOURCE_CODE["unknown"]
NO_DOCUMENT = ne_format.DESC_DOC_UNKNOWN


def _tok(k):
    """A view token spelled the way the state spells one -- 16 bytes, base64,
    percent-encoded -- because the carry path rebuilds it from the 16 raw bytes
    in the previous build's token blocks, and a made-up string would not
    survive that round trip."""
    return quote(base64.b64encode(bytes([k]) * ne_format.TOKEN_BYTES).decode(), safe="")


def _record(tokens, key="k1"):
    """A documents.jsonl entry for one record publishing these documents."""
    return {"k": key, "doc": "CW8847", "entity": "E", "n": len(tokens),
            "documents": [{"name": f"DOC{i}", "size": "1Mb", "token": t}
                          for i, t in enumerate(tokens)]}


def _previous_build(tmp_path, view_tokens, descriptions, sources=None, documents=None,
                    lists=None):
    """Leave behind the payload an earlier build_site.main() would have.

    Written with the same ne_format writers main() uses, so the carry path
    reads real files. `sources`, `documents` and `lists` left as None are not
    written at all, which is what a payload made before descsrc.bin, descdoc.bin
    or the xdoc blocks existed looks like.
    """
    n = len(view_tokens)
    outdir = str(tmp_path / "d" / "prev")
    columns = {name: array.array("i", [0] * n) for name in ne_format.I32_COLUMNS}
    columns.update({name: array.array("d", [0.0] * n) for name in ne_format.F64_COLUMNS})
    columns.update({name: array.array("B", [0] * n) for name in ne_format.U8_COLUMNS})
    columns["viewPresent"] = array.array("B", [1 if t else 0 for t in view_tokens])
    columns["docLen"] = array.array("B", [1] * n)
    ne_format.write_payload(outdir, columns, [b"D"] * n, [b"V"], [b"\x00" * 16],
                            {"count": n, "vendorCount": 1, "descCount": len(descriptions)})
    view = [base64.b64decode(unquote(t)) if t else b"" for t in view_tokens]
    ne_format.write_token_blocks(outdir, [b""] * n, view, n)
    ne_format.write_desc_blocks(outdir, descriptions, n)
    if sources is not None:
        ne_format.write_desc_sources(outdir, sources, n)
    if documents is not None:
        ne_format.write_desc_documents(outdir, documents, n)
    if lists is not None:
        ne_format.write_xdoc_blocks(outdir, {
            row: ne_format.pack_documents([
                {"token": base64.b64decode(unquote(t)), "name": f"DOC{i}", "size": "1Mb"}
                for i, t in enumerate(tokens)])
            for row, tokens in lists.items()}, n)
    (tmp_path / "manifest.json").write_text(json.dumps({"buildId": "prev", "dir": "d/prev"}))


def _carry(monkeypatch, tmp_path, view_tokens, docs_entries, scope_records):
    """load_descriptions as tonight's build runs it: the previous payload on disk."""
    monkeypatch.setattr(build_site, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(build_site, "ROOT", str(tmp_path))
    monkeypatch.setattr(build_site, "SCOPE_JSONL", _scope(tmp_path, scope_records))
    (tmp_path / "data").mkdir(exist_ok=True)
    os.replace(_documents(tmp_path, docs_entries), str(tmp_path / "data" / "documents.jsonl"))
    return build_site.load_descriptions(view_tokens, carry=True)


# CW8847 at the University of Nebraska Kearney: five documents, the first says
# nothing, and "Elevator service agreement" is the cover sheet of the third.
CW8847 = [_tok(1), _tok(2), _tok(3), _tok(4), _tok(5)]
CW8847_SCOPE = [
    {"tok": CW8847[2], "doc": "CW8847", "source": "cover_sheet",
     "description": "Elevator service agreement"},
    {"tok": CW8847[3], "doc": "CW8847", "source": "cover_sheet",
     "description": "Elevator service agreement for UNK campus. Amendment #2"},
]


def test_a_fresh_build_then_a_carried_one_publish_the_same_provenance(monkeypatch, tmp_path):
    """The nightly sequence, end to end: a fresh build, its payload restored,
    then tonight's build carrying it forward against the same scope.jsonl.
    Nothing changed in between, so nothing published may change either."""
    view_tokens = [CW8847[0], _tok(9)]
    entries = [_record(CW8847)]
    scope = CW8847_SCOPE + [{"tok": _tok(9), "doc": "P1", "source": "line_items",
                             "description": "ORDINARY ROW"}]

    fresh = _run(monkeypatch, tmp_path, view_tokens, entries, scope)
    assert fresh[0][0] == b"Elevator service agreement"
    assert (fresh[1][0], fresh[2][0]) == (COVER_SHEET, 2)

    descriptions, sources, documents = fresh
    _previous_build(tmp_path, view_tokens, descriptions, sources, documents,
                    lists={0: CW8847})
    carried = _carry(monkeypatch, tmp_path, view_tokens, entries, scope)

    assert carried == fresh


def test_a_carried_later_document_keeps_its_source_and_place(monkeypatch, tmp_path):
    """With nothing in scope.jsonl for the row, the carried copy is all there is,
    so it has to carry its own provenance rather than the first document's."""
    _previous_build(tmp_path, [CW8847[0]], {0: b"Elevator service agreement"},
                    {0: COVER_SHEET}, {0: 2}, lists={0: CW8847})
    descriptions, sources, documents = _carry(
        monkeypatch, tmp_path, [CW8847[0]], [_record(CW8847)], [])
    assert descriptions[0] == b"Elevator service agreement"
    assert sources[0] == COVER_SHEET
    assert documents[0] == 2


def test_a_carried_row_ranks_as_the_document_it_came_from(monkeypatch, tmp_path):
    """Its rank is the rank of the record that described it: a later document
    does not displace it, and the record's own first document still does."""
    _previous_build(tmp_path, [CW8847[0]], {0: b"FROM THE THIRD"},
                    {0: COVER_SHEET}, {0: 2}, lists={0: CW8847})
    later = [{"tok": CW8847[4], "doc": "CW8847", "source": "cover_sheet",
              "description": "FROM THE FIFTH"}]
    descriptions, _sources, documents = _carry(
        monkeypatch, tmp_path, [CW8847[0]], [_record(CW8847)], later)
    assert (descriptions[0], documents[0]) == (b"FROM THE THIRD", 2)

    first = [{"tok": CW8847[0], "doc": "CW8847", "source": "cover_sheet",
              "description": "THE CONTRACT ITSELF"}]
    descriptions, _sources, documents = _carry(
        monkeypatch, tmp_path, [CW8847[0]], [_record(CW8847)], first)
    assert (descriptions[0], documents[0]) == (b"THE CONTRACT ITSELF", 0)


def test_a_carried_drifted_primary_is_still_the_primary(monkeypatch, tmp_path):
    """The CSV's document no longer sorts first. Its description is still the
    primary's and outranks the newcomer ahead of it -- which a rank worked out
    from the position alone (1 + 1) would not."""
    listed = [_tok(7), _tok(8)]                      # newcomer, then the CSV's own
    _previous_build(tmp_path, [listed[1]], {0: b"OLD"}, {0: COVER_SHEET}, {0: 1},
                    lists={0: listed})
    newcomer = [{"tok": listed[0], "doc": "D1", "source": "cover_sheet",
                 "description": "NEW"}]
    descriptions, _sources, documents = _carry(
        monkeypatch, tmp_path, [listed[1]], [_record(listed)], newcomer)
    assert (descriptions[0], documents[0]) == (b"OLD", 1)


def test_a_carried_position_follows_its_document_when_the_list_reorders(monkeypatch, tmp_path):
    """A document filed since the last build sorts ahead of the source. The
    position is the state's order today, which is what the page's list shows,
    so a carried position copied as-is would mark the document beside it."""
    _previous_build(tmp_path, [CW8847[0]], {0: b"Elevator service agreement"},
                    {0: COVER_SHEET}, {0: 2}, lists={0: CW8847})
    today = CW8847[:1] + [_tok(6)] + CW8847[1:]      # a new second document
    descriptions, sources, documents = _carry(
        monkeypatch, tmp_path, [CW8847[0]], [_record(today)], CW8847_SCOPE)
    assert descriptions[0] == b"Elevator service agreement"
    assert (sources[0], documents[0]) == (COVER_SHEET, 3)


def test_a_carried_single_document_row_keeps_position_zero(monkeypatch, tmp_path):
    """About 95% of rows publish one document and ship no xdoc list, so the
    carry has to recover their source from the row's own View URL. With
    nothing in scope.jsonl for the row, the carried copy must still name
    document 0 -- not the unknown marker, which would drop a true claim."""
    _previous_build(tmp_path, [_tok(9)], {0: b"ORDINARY ROW"},
                    {0: COVER_SHEET}, {0: 0}, lists={})
    descriptions, sources, documents = _carry(monkeypatch, tmp_path, [_tok(9)], [], [])
    assert descriptions[0] == b"ORDINARY ROW"
    assert (sources[0], documents[0]) == (COVER_SHEET, 0)


def test_an_older_payload_without_provenance_claims_no_document(monkeypatch, tmp_path):
    """A payload built before descsrc.bin and descdoc.bin kept only the text. Its
    source is unknown and so is its document: 0 would be a claim, and the page
    prints "description read from this one" beside whichever document the byte
    names. The text itself stays -- the previous build is still the floor."""
    _previous_build(tmp_path, [CW8847[0]], {0: b"Elevator service agreement"})
    descriptions, sources, documents = _carry(
        monkeypatch, tmp_path, [CW8847[0]], [_record(CW8847)], [])
    assert descriptions[0] == b"Elevator service agreement"
    assert sources[0] == UNKNOWN
    assert documents[0] == NO_DOCUMENT


def test_a_build_that_lost_its_provenance_is_corrected_by_scope(monkeypatch, tmp_path):
    """The live payload's own state: carried rows labelled "unknown" beside a
    position 0 that was never recorded, only assumed. Trusting that 0 would rank
    the row as the primary and keep outranking the record that could fix it,
    every night, so an "unknown" row is placed nowhere and scope.jsonl decides."""
    _previous_build(tmp_path, [CW8847[0]], {0: b"Elevator service agreement"},
                    {0: UNKNOWN}, {0: 0}, lists={0: CW8847})
    descriptions, sources, documents = _carry(
        monkeypatch, tmp_path, [CW8847[0]], [_record(CW8847)], CW8847_SCOPE)
    assert descriptions[0] == b"Elevator service agreement"
    assert (sources[0], documents[0]) == (COVER_SHEET, 2)


def test_the_page_can_never_mark_the_unknown_document():
    """DESC_DOC_UNKNOWN works only because index.html marks the document whose
    index equals the byte, among the few it lists. Listing that many would
    turn "we do not know" back into a claim about one of them."""
    page = open(os.path.join(os.path.dirname(SCRIPTS), "index.html"), encoding="utf-8").read()
    shown = int(re.search(r"var SHOWN = (\d+);", page).group(1))
    assert "i === from" in page
    assert shown <= NO_DOCUMENT
