"""The description parsers, against the document shapes that actually occur.

Every fixture here is trimmed from a real document. That matters: both bugs
this file exists to prevent came from assuming one layout when the state uses
two, and neither would have been caught by a fixture invented from the code.

What these parsers emit is published verbatim as the state's own words, so a
wrong extraction is not a formatting problem -- it is putting one contract's
description next to another contract's money.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import extract_scope  # noqa: E402


# --- University cover sheet -------------------------------------------------

# The Procure-to-Pay page. The label wraps across lines in the extracted text,
# which is why the pattern matches on its tail rather than the whole phrase.
COVER_SHEET = """
University of Nebraska Contract Summary

CONTRACTOR/COMPANY INFORMATION
Supplier DVSPORT INC Contact Kenneth Brown

CONTRACT DESCRIPTION/INFORMATION
Contract Summary
(brief description
and/  or event  name)
Coaches Replay system to be used by MBB.
Contract Document Document Amt Total Amt Start Date  End Date Notes
Master Agreement $4,500.00 $4,500.00 10/01/2025 9/30/2026
"""

# The second layout. It closes the field with different labels entirely, and
# matching only the first set ran the capture past the end of the field and on
# through the dates and dollar amounts -- one description came back 19,405
# characters long.
COVER_SHEET_SECOND_LAYOUT = """
University of Nebraska Contract Summary
Contract Summary
(brief description
and/  or event  name)
Amendment to renew January titles for another 2-years.
Purchase Category Office, Shipping & Other Total Amount of Spend $145,141 USD
Start Date 1/1/2025 12:00 AM End Date 12/31/2026 11:59 PM BID INFORMATION
"""


def test_cover_sheet_description():
    assert extract_scope.from_cover_sheet(COVER_SHEET) == \
        "Coaches Replay system to be used by MBB."


def test_second_cover_sheet_layout_stops_at_its_own_labels():
    got = extract_scope.from_cover_sheet(COVER_SHEET_SECOND_LAYOUT)
    assert got == "Amendment to renew January titles for another 2-years."
    # The regression: the capture must not swallow the fields below it.
    assert "Purchase Category" not in got
    assert "145,141" not in got


def test_no_cover_sheet_returns_none():
    assert extract_scope.from_cover_sheet("just some contract text") is None


def test_cover_sheet_present_but_empty_returns_none():
    empty = COVER_SHEET.replace("Coaches Replay system to be used by MBB.", "")
    assert extract_scope.from_cover_sheet(empty) is None


# --- University purchase orders ---------------------------------------------

# Zero-padded line number, whole quantity, alphabetic unit, two-decimal money.
# The description column is a fixed 40 characters, which is where the mid-word
# truncation comes from -- "Keyboar" is the state's cut, not ours.
UNIVERSITY_PO = (
    "Your ref.:ARIBA_P2P "
    "001 6 EA 1/4 in. Square Head and Solid Domestic B 3.53 21.18 "
    "002 3 PKG Logitech MK540 Advanced Wireless Keyboar 14.97 44.91 "
)


def test_university_line_items():
    assert extract_scope.from_line_items(UNIVERSITY_PO) == [
        "1/4 in. Square Head and Solid Domestic B",
        "Logitech MK540 Advanced Wireless Keyboar",
    ]


def test_repeated_identical_items_appear_once():
    doubled = UNIVERSITY_PO + "003 6 EA 1/4 in. Square Head and Solid Domestic B 3.53 21.18 "
    assert extract_scope.from_line_items(doubled).count(
        "1/4 in. Square Head and Solid Domestic B") == 1


# --- State agency purchase orders -------------------------------------------

# A different form entirely: plain line number, four-decimal quantity and unit
# price, a unit that is often "$", and no width limit -- so a long description
# resumes *after* the money columns and the text between one row and the next
# belongs to the row before it.
STATE_PO = """State of Nebraska Purchase Order
Line Description Quantity
Unit of
Measure
Unit
Price
Extended
Price
1 LABOR FOR BUILDING 14'X20' 5,000.0000 $ 1.0000 5,000.00
STORAGE GARAGE

Total Order 5,000.00
"""

STATE_PO_MULTI = """State of Nebraska Purchase Order
Line Description Quantity
1 20X30 MOUNTED ARCHIVAL PRINTS 31.0000 EA 215.0000 6,665.00

2 MOUNTING HANGING HARDWARE 31.0000 EA 10.0000 310.00

Total Order 6,975.00
"""


def test_state_wrapped_description_is_reattached():
    assert extract_scope.from_line_items(STATE_PO) == [
        "LABOR FOR BUILDING 14'X20' STORAGE GARAGE"
    ]


def test_state_multiple_items():
    assert extract_scope.from_line_items(STATE_PO_MULTI) == [
        "20X30 MOUNTED ARCHIVAL PRINTS",
        "MOUNTING HANGING HARDWARE",
    ]


def test_state_descriptions_are_not_capped_at_forty():
    """Unlike the University form, the state's column has no width limit."""
    long_item = "A VERY LONG DESCRIPTION THAT KEEPS GOING WELL PAST FORTY CHARACTERS"
    text = ("State of Nebraska Purchase Order Line Description Quantity "
            f"1 {long_item} 1.0000 EA 5.0000 5.00 Total Order 5.00")
    assert extract_scope.from_line_items(text) == [long_item]


def test_university_form_wins_when_both_patterns_could_fire():
    """A document carrying both must not be parsed twice or by the wrong rule."""
    assert extract_scope.from_line_items(UNIVERSITY_PO + STATE_PO) == \
        extract_scope.from_line_items(UNIVERSITY_PO)


# --- collapsed layouts ------------------------------------------------------

def test_collapsed_columns_do_not_produce_one_giant_item():
    """Some documents have a text layer whose column structure has fallen apart:
    every description runs together and every number lands at the end. An
    unbounded capture spanned hundreds of characters of that and called it one
    line item."""
    wreckage = ("State of Nebraska Purchase Order Line Description "
                + "ESU 7 FULLERTON HS NON RECURRING COST 40 TO 200 " * 12
                + "1.0000 EA 1.0000 1.00 Total Order 1.00")
    for item in extract_scope.from_line_items(wreckage):
        assert len(item) <= 120, f"unbounded capture: {len(item)} chars"


# --- precedence and the whole-document entry point --------------------------

def test_cover_sheet_beats_line_items():
    """A contract with a purchase order attached carries both. The sentence a
    person wrote beats a list of part numbers."""
    source, description, items = extract_scope.describe(COVER_SHEET + UNIVERSITY_PO)
    assert source == "cover_sheet"
    assert description == "Coaches Replay system to be used by MBB."
    assert items == []


def test_describe_returns_none_when_nothing_matches():
    assert extract_scope.describe("UNIVERSITY OF NEBRASKA-LINCOLN STANDARD AGREEMENT") is None


# --- the append-only log ----------------------------------------------------

def test_only_the_newest_entry_per_document_is_used(tmp_path):
    """doc_text.jsonl is append-only, so a document re-fetched after a fix
    appears twice and only the last entry counts -- the rule its own loader
    already applies. Reading straight through described 24 documents twice,
    from two different versions of their text."""
    path = tmp_path / "doc_text.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for text in ("old text", "new text"):
            f.write(json.dumps({"tok": "SAME", "data": {"status": "text", "text": text}}) + "\n")
        f.write(json.dumps({"tok": "OTHER", "data": {"status": "text", "text": "x"}}) + "\n")

    current = extract_scope.current_lines(str(path))
    assert current == {1, 2}, "line 0 is superseded by line 1"


# --- the University's wrapped description column ----------------------------

# Trimmed from document 4740007268, a $15,027,565.88 Hausmann Construction PO.
# Reported by a reader who checked it against the source: the site showed
# "GENERAL CONSTRUCTION SERVICES FOR" and stopped.
DEVANEY = """FAX TO 402-438-3235, ATTN: JOEY HAUSMANN
001          1 PU  GENERAL CONSTRUCTION SERVICES FOR 15,027,565.88 15,027,565.88
REMODEL OF BOB DEVANTEY SPORTS CENTER
PER UNL INVITATION TO BID
909353-12.
"""

# Trimmed from a Schemmer Associates PO. Nothing here continues the
# description: pdf extraction emits page blocks in its own order, so the
# vendor address block and then the table header land directly under the row.
FURNITURE_FOLLOWS = """Your ref.:ARIBA_P2P
001          1 PU  Per NTP dated  SOW: UNL Eastern Nebraska  83,150.00 83,150.00
Your material number: NA
Vendor: 108180
SCHEMMER ASSOC INC
1044 N 115 ST STE 300
OMAHA NE  68154
"""


def test_a_wrapped_description_is_read_to_its_end():
    """The column is 40 characters wide and the text wraps inside it rather
    than being cut, so reading only the line carrying the money columns
    truncated 93% of University items. The state's spelling of Devaney is
    theirs and is kept."""
    items = extract_scope.from_line_items(DEVANEY)
    assert items == ["GENERAL CONSTRUCTION SERVICES FOR REMODEL OF BOB DEVANTEY "
                     "SPORTS CENTER PER UNL INVITATION TO BID 909353-12."]


def test_page_furniture_does_not_become_part_of_the_description():
    """The line after the row is not automatically a continuation. Appending
    blindly here would put a vendor's postal address inside the description of
    what the university bought."""
    items = extract_scope.from_line_items(FURNITURE_FOLLOWS)
    assert items == ["Per NTP dated SOW: UNL Eastern Nebraska"]
    assert "OMAHA" not in items[0] and "SCHEMMER" not in items[0]


def test_the_next_row_ends_the_previous_description():
    text = ("001          1 PU  FIRST ITEM DESCRIPTION 1,000.00 1,000.00\n"
            "CONTINUES HERE\n"
            "002          2 EA  SECOND ITEM 2,000.00 4,000.00\n")
    assert extract_scope.from_line_items(text) == [
        "FIRST ITEM DESCRIPTION CONTINUES HERE", "SECOND ITEM"]


def test_a_collapsed_text_layer_still_falls_back_to_the_flat_pattern():
    """A few documents extract with their line breaks gone, so nothing anchors
    to a line end. Those must not silently yield nothing."""
    collapsed = ("Line Description 001 1 PU  SOME COLLAPSED ITEM 500.00 500.00 "
                 "002 1 PU  ANOTHER ITEM 600.00 600.00")
    items = extract_scope.from_line_items(collapsed)
    assert "SOME COLLAPSED ITEM" in items


def test_the_continuation_cap_is_insurance_and_should_not_be_doing_the_cutting():
    """At 12 lines the cap decided where a quarter of descriptions ended, which
    is the parser choosing the boundary rather than the document. Measured at
    25 it never binds. This asserts the property, not the number."""
    assert extract_scope.MAX_CONTINUATION_LINES >= 25


# --- the state agency form's wrapped tail -----------------------------------

def _state(rows):
    return "Line Description " + rows


def test_a_numeric_continuation_is_no_longer_thrown_away():
    """The old rule dropped any tail containing a digit, which lost 892 of
    1,367 tails. Half of what people type into a description has a number in
    it: sizes, percentages, container counts."""
    text = _state("1 MILK, CHOCOLATE 1.0000 EA 0.4200 0.42 1/2 PINT/CONTAINER, 1%")
    assert extract_scope.state_items(extract_scope.flatten(text)) == [
        "MILK, CHOCOLATE 1/2 PINT/CONTAINER, 1%"]


def test_a_tail_that_is_really_the_next_row_is_still_rejected():
    """The case the digit rule existed for, and the reason the replacement
    keys on four-decimal columns rather than on digits. This row has a negative
    unit price, so STATE_ITEM does not match it and it arrives as a tail."""
    text = _state("1 FREIGHT 1.0000 EA 0.1900 0.19 "
                  "4 OFFICE SUPPLIES EXPENSE 1.0000 EA -0.1900 -0.19")
    items = extract_scope.state_items(extract_scope.flatten(text))
    assert items == ["FREIGHT"]
    assert "OFFICE SUPPLIES" not in items[0]


def test_a_tail_running_into_page_furniture_is_cut_at_it():
    """Trimmed from a real document: the description is the word before the
    DocuSign stamp, and everything after it belongs to the next page."""
    text = _state("1 MONTHLY PER PORT 1.0000 EA 5.0000 5.00 Estimated "
                  "DocuSign Envelope ID: 6522138C STATE OF NEBRASKA")
    assert extract_scope.state_items(extract_scope.flatten(text)) == [
        "MONTHLY PER PORT Estimated"]


def test_the_state_continuation_cap_sits_above_the_measured_p99():
    """At 80 the cap was below the 99th percentile of genuine tails (293), so
    it was cutting real descriptions. Asserts the property, not the number."""
    assert extract_scope.MAX_CONTINUATION >= 300
