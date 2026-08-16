"""Builder helpers, and the two guards that protect readers from silence.

The bugs worth preventing here do not raise. `incomplete_coverage()` returning
everything looks like a cautious warning rather than a fault. A permalink
collision opens the wrong contract without erroring. A description carried onto
the wrong row shows one contract's words beside another's money. None of these
announce themselves, which is exactly why they need tests.
"""

import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import build_site  # noqa: E402


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
    assert build_site.find_permalink_collision(docs, entity, type_) is None


def test_repeated_triple_is_found():
    docs = [b"100", b"200", b"100"]
    entity = [0, 1, 0]
    type_ = [5, 5, 5]
    first, second, triple = build_site.find_permalink_collision(docs, entity, type_)
    assert (first, second) == (0, 2)
    assert triple == (b"100", 0, 5)


def test_same_document_number_across_types_is_not_a_collision():
    """14,633 document numbers are reused; type is part of what separates them."""
    assert build_site.find_permalink_collision([b"A", b"A"], [0, 0], [1, 2]) is None


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
