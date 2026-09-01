"""Pull the description the state already wrote out of the documents it publishes.

The database exposes no description field anywhere -- a row is a document
number, a vendor, an amount and two dates. What a contract is actually *for*
exists only inside the PDF. But for most records it is already written there
in plain English, by the person who filed the document, and can be lifted
verbatim. Nothing here is generated or summarized; every string this script
emits is the state's own text.

Several sources, in descending order of how much they tell you:

  * A **cover sheet**. University of Nebraska contracts carry a Procure-to-Pay
    page whose "Contract Summary (brief description and/or event name)" field
    is filled in by hand: "Coaches Replay system to be used by MBB." This is
    the closest thing to a scope of work the state publishes.

  * A **line-item table**. Purchase orders list what was bought. These are
    item descriptions rather than scopes of work, and they carry the
    administrative text people type alongside them -- invoicing instructions,
    project numbers, change-order logs -- which is kept, because on some
    documents the scope appears after the boilerplate.

    The University's column is 40 characters wide and the text *wraps* inside
    it. It does not cut. This file used to say the opposite, and both parsers
    were built to that belief: they read only the line carrying the money
    columns and dropped every continuation, truncating 93% of University items
    and discarding 892 of 1,367 state tails. A reader caught it by comparing a
    $15 M purchase order against its source PDF. Do the same before trusting
    any change here -- comparing output to output cannot see this class of bug.

  * The **State Purchasing Bureau's opening sentence**, on the award and
    purchase-order forms state agencies file: "Contract to supply and deliver
    Fine Gradation Brining Salt to the State of Nebraska." The state agency
    side of the database has no cover sheet, and this is the nearest thing.

  * The **direct-purchase notice**, which describes no work at all. Roads files
    it instead of a document when it bought something outright, and it is here
    because the state saying "there is no contract for this item" is a better
    answer than our own "no description could be read".

Reads whatever text scripts/extract_text.py has already captured and writes
one record per document it can describe. It makes no network requests, so it
is safe to re-run at any time -- and running it against the text already on
disk is how you judge whether the descriptions are worth collecting the rest.

    python3 scripts/extract_scope.py            # extract, write, report
    python3 scripts/extract_scope.py --sample 20  # read 20 real results
"""

import argparse
import collections
import json
import os
import random
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IN_JSONL = os.path.join(ROOT, "data", "doc_text.jsonl")
OUT_JSONL = os.path.join(ROOT, "data", "scope.jsonl")

# The cover sheet's label wraps across several lines in the extracted text
# ("Contract Summary / (brief description / and/  or event  name)"), so match
# on its tail with the whitespace collapsed.
#
# Two cover-sheet layouts exist and they close the field with different labels;
# without the full set the capture runs on into the rest of the form and picks
# up dates and dollar amounts. Non-greedy, so whichever label comes first wins.
COVER_SHEET = re.compile(
    r"event\s*name\)\s*(.*?)\s*(?:"
    r"Contract Document|COMPETITION/BID|INTERNAL USE ONLY|"
    r"Purchase Category|BID INFORMATION|Total Amount of Spend|"
    r"Total Amendment Amount|Start Date"
    r")",
    re.S,
)

# A numbered SERVICES or SCOPE OF SERVICES clause, on contract templates that
# carry no cover sheet at all. Bounded at the next numbered heading, which is
# what stops it swallowing DELIVERY, COMPENSATION and the rest of the contract.
#
# Worth knowing how little this reaches. Of 32,378 readable documents that no
# other pattern describes, this one covers 1,494 -- 4.6%. The rest are email
# threads, signature pages, notices to proceed and change-order stubs that
# contain no description of the work anywhere. Three other candidates were
# measured and rejected: a "Project:" line (177 documents), an "engages" clause
# (647, and entirely inside the ones below), and an "RE:" line (21, mostly
# email subjects, which is a different thing wearing a description's clothes).
SERVICES_CLAUSE = re.compile(
    # "SCOPE OF SERVICES" is a heading wherever it appears, numbered or not --
    # the professional-services template writes "SCOPE OF SERVICES 1.1 The
    # Architect...", with the number after the heading rather than before it.
    # A bare "SERVICES" is only a heading when a clause number introduces it:
    # unanchored it also matches inside "PROFESSIONAL SERVICES AGREEMENT".
    r"(?:(?:\b\d{1,2}(?:\.\d+)?\.?\s+)?(?:SCOPE OF SERVICES|SCOPE OF WORK)"
    r"|\b\d{1,2}(?:\.\d+)?\.\s+SERVICES)"
    r"\b[:.]?\s+"
    # A clause number belonging to the body rather than to the heading.
    r"(?:\d{1,2}(?:\.\d+)+\s+)?"
    r"(.{40,1200}?)(?=\s+\d{1,2}(?:\.\d+)?\.\s+[A-Z]{3}|$)", re.S)

# Purchase orders come on two different forms and neither pattern matches the
# other's layout. Anchoring on the trailing money columns is what makes the
# description boundary unambiguous in both.

# University: zero-padded line number, whole-number quantity, an alphabetic
# unit code. The description column is 40 characters wide and *wraps* inside
# that width -- it does not cut, which is what university_items() reads to the
# end of.
#
# The unit-price column carries two OR four decimals ("2.62 26.20" but also
# "0.1580 15.80" where the unit price is fractions of a cent). Allowing only
# two silently skipped every fourth-decimal row: a Mouser order for ceramic
# capacitors lost its parts entirely and kept only "SHIPPING". The total column
# is always two.
UNIVERSITY_ITEM = re.compile(
    r"\b\d{3}\s+\d[\d,]*\s+[A-Z]{2,4}\s+(.{3,120}?)\s+[\d,]+\.\d{2,4}\s+[\d,]+\.\d\d"
)

# The same row, anchored to a whole line, so the lines that follow can be read
# as the description column continuing. Used in preference to the pattern above
# wherever the text layer still has its line breaks.
#
# The total column is optional because a no-charge row prints only one figure
# ("001 1 EA ABHD5 Rabbit pAb - 100 L 0.00"). Requiring both dropped every free
# line item -- on an antibody order that meant losing the samples and keeping
# only what was invoiced. Optional only here: the flattened fallback has no
# line ends to stop it, so a single money column there would run on.
UNIVERSITY_LINE = re.compile(
    r"^\s*\d{3}\s+\d[\d,]*\s+[A-Z]{2,4}\s+(.{3,120}?)\s+[\d,]+\.\d{2,4}(?:\s+[\d,]+\.\d\d)?\s*$"
)

# Lines that follow an item without belonging to its description. PDF text
# extraction emits page blocks in an order of its own, so the vendor address
# and even the table header can land directly under a row. Built by counting
# the most common line after an item across 40,000 documents rather than
# guessed: "Your material number" alone accounts for ~2,400 of them.
FURNITURE = re.compile(
    r"^\s*(?:"
    r"Your\s+(?:material\s+number|ref\.?)"
    r"|Vendor:\s*\d"
    r"|MATERIAL\b|NUMBERLN#|LN#\s+QTY"
    r"|Please\s+Deliver\s+To:"
    r"|Valid\s+(?:From|To):|Delivery\s+Date:|Destination\b|F\.O\.B\."
    r"|INSTRUCTIONS\s+AND\s+CONDITIONS|Tracking\s+Number:"
    r"|Total\s+(?:Order|Cost)|Sub-?total"
    r"|Page\s+\d"
    r")", re.I)

# Insurance against a runaway, not a truncator. Measured over 13,127 items: at
# 12 lines the cap was doing the cutting 26% of the time, which is the parser
# choosing where the state's sentence ends. At 25 it never binds -- furniture or
# the next row always stops it first -- and the longest result is 887 characters
# against MAX_DESCRIPTION's 4,000. If this ever starts binding again, something
# about the form has changed and the cut belongs in FURNITURE instead.
MAX_CONTINUATION_LINES = 25

# State agencies: plain line number, four-decimal quantity and unit price, and
# a unit that is often "$" rather than a code. Its description column is not
# fixed-width, so these run longer than the University's.
# The 120-character bound is load-bearing, not cosmetic. A handful of documents
# have a text layer whose column structure has collapsed -- every description
# runs together and every number lands at the end -- and an unbounded capture
# happily spans hundreds of characters of that wreckage and calls it one item.
STATE_ITEM = re.compile(
    r"(?:^|\s)\d{1,3}\s+(.{3,120}?)\s+[\d,]+\.\d{4}\s+\S{1,4}\s+[\d,]+\.\d{4}\s+[\d,]+\.\d\d"
)

# The state's table header. Matching from here rather than the whole document
# keeps the row pattern away from the addresses and boilerplate above it, which
# carry enough numbers to look like line items.
STATE_TABLE = "Line Description"

# A state description that outruns the money columns resumes *after* them, so
# the text between one row and the next belongs to the row before it
# ("LABOR FOR BUILDING 14'X20'" ... "STORAGE GARAGE").
#
# The old test for "is this tail actually the next row" was "does it contain a
# digit", which threw away 892 of 1,367 tails: "MILK, CHOCOLATE" lost "1/2
# PINT/CONTAINER, 1%", "ICE CREAM" lost "SOFT SERVE, VANILLA, 1/2 GALLO". Half
# of what people write into a description column has a number in it.
#
# What actually marks the next row is its four-decimal quantity and unit-price
# columns. Nobody types "1.0000" into a description, and every genuine
# next-row-in-the-tail case carries one -- including the negative-priced rows
# that STATE_ITEM itself misses, which is how they end up in a tail at all.
ROW_COLUMNS = re.compile(r"\d[\d,]*\.\d{4}")

# The state's page furniture, for tails that run off the end of the table into
# the next page's letterhead. Cut the tail here rather than dropping it: the
# document that produced this list reads "MONTHLY PER PORT Estimated" and then
# a DocuSign stamp, and the first two words are the description.
STATE_FURNITURE = re.compile(
    r"DocuSign Envelope ID|STATE OF NEBRASKA|State Purchasing Bureau"
    r"|SERVICE CONTRACT AWARD|Page \d+ of \d+")

# Insurance only, after ROW_COLUMNS and STATE_FURNITURE have done the real
# work. Measured across 1,367 tails: median 13 characters, p99 293. The old
# value of 80 sat below the p99 and was silently cutting genuine descriptions.
MAX_CONTINUATION = 400

# Long enough for a paragraph someone genuinely typed, short enough that a
# runaway capture is obvious rather than shipped. Nine documents in the current
# text exceed the old boundary set; this is the backstop if a third layout
# turns up.
MAX_DESCRIPTION = 4000


def flatten(s):
    return " ".join(s.split())


def current_lines(path):
    """Line numbers holding the newest entry per document.

    doc_text.jsonl is an append-only log, so a document re-fetched after a bug
    fix appears more than once and only the last entry counts -- the same rule
    its own loader applies. Reading the file straight through instead would
    describe such a document twice, from two different versions of its text.

    Two passes rather than a dict of parsed records: at 691,145 documents the
    records are gigabytes and the line numbers are megabytes.
    """
    newest = {}
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f):
            if line.strip():
                newest[json.loads(line)["tok"]] = number
    return set(newest.values())


def from_cover_sheet(text):
    """The hand-written contract summary, or None."""
    if "Contract Summary" not in text:
        return None
    match = COVER_SHEET.search(text)
    if not match:
        return None
    value = flatten(match.group(1))
    return value or None


# The University's other cover sheet: a filled PDF form, not prose. It defeats
# COVER_SHEET entirely because the form is flattened on export -- every field
# label is emitted first, in a block, and only then every filled value, in the
# same order. "DESCRIPTION OF PURCHASE" and its answer end up dozens of lines
# apart, so there is nothing for a label-anchored pattern to grab.
#
# What survives is the ordering: the value after the supplier's name is the
# description. That is positional, which is exactly the kind of assumption that
# quietly attributes one contract's words to another, so it is checked rather
# than trusted -- see the verification note in README.md. Empty fields are
# omitted from the value run, which is the failure mode the guards below exist
# to catch: with the description blank, the term dates slide into its place.
FORM_LABELS = {
    "UNIVERSITY OF NEBRASKA", "CONTRACT COVER SHEET", "CONTRACT TYPE", "EXPENDITURE",
    "FEE-FOR-SERVICE", "IT", "OTHER", "SUPPLIER NAME", "DESCRIPTION OF", "PURCHASE",
    "DESCRIPTION OF PURCHASE", "TERM START AND END DATE", "TERM LENGTH", "DOLLAR AMOUNT",
    "BUYER NAME/OWNER", "BUYER NAME", "PURCHASED FOR", "(DEPARTMENT)?", "BID#",
    "SOLE SOURCE", "BOR REPORTABLE?", "BOR DATE", "RENEWAL REMINDERS TO:", "NOTES:",
    "COMMODITY TYPE", "IANR BUSINESS CENTER", "UNIVERSITY SIGNER", "ZERO DOLLAR LEASE",
    "FUNDING SOURCE", "REVENUE", "NON-EXPENDITURE", "AMENDMENT", "CONTRACT",
}
FORM_REVISED = re.compile(r"^Revised\s+[\d/\.\-]+$", re.I)
FORM_CHECK = {"\u2714", "\u2713", "X", "x", "N/A", "n/a", "\x14"}
FORM_MONEY = re.compile(r"\$\s?[\d,]+(?:\.\d\d)?")
FORM_TERM = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}|\b\d+\s+(?:year|month|day)s?\b|"
                       r"\b(?:annual|ongoing|perpetual|one[- ]time)\b", re.I)
# "9/28/2020 - 9/27/2023" and friends. Three real misreads in a sample of 40 were
# exactly this: a blank description field, and the term picked up in its place.
FORM_DATE_RANGE = re.compile(
    r"^\s*(?:\S.*?)?\d{1,2}/\d{1,2}/\d{2,4}\s*(?:-|\u2013|to|through)\s*"
    r"\d{1,2}/\d{1,2}/\d{2,4}\s*$", re.I)


def form_readable(value):
    """False for text the PDF's font encoding defeated.

    One document in the same sample extracted as '/\x04EZ\x03\x11h^/E\x1c^^' where
    the page plainly reads 'travel'. Publishing that as the state's own words is
    worse than publishing nothing, and no later check would have caught it.
    """
    if not value:
        return False
    if any(ord(c) < 32 for c in value):
        return False
    letters = sum(c.isalpha() or c.isspace() for c in value)
    return letters / len(value) >= 0.6


def from_cover_sheet_form(text):
    """The DESCRIPTION OF PURCHASE field off the flattened form, or None."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 10:
        return None
    if "CONTRACT COVER SHEET" not in " ".join(lines[:4]).upper():
        return None

    # The label run ends where lines stop being labels; a stray revision stamp
    # sits among them, so allow a short gap before calling it finished.
    last_label = -1
    for i, line in enumerate(lines[:40]):
        if line.upper() in FORM_LABELS or FORM_REVISED.match(line):
            last_label = i
        elif i - last_label > 3:
            break
    if last_label < 4:
        return None

    values = [l for l in lines[last_label + 1:last_label + 12]
              if l not in FORM_CHECK and not FORM_REVISED.match(l)
              and l.upper() not in FORM_LABELS]
    if len(values) < 2:
        return None

    value = values[1].strip()
    if not (3 <= len(value) <= 300):
        return None
    if FORM_MONEY.fullmatch(value) or value.isdigit() or FORM_TERM.fullmatch(value):
        return None
    if FORM_DATE_RANGE.match(value) or not form_readable(value):
        return None
    if value.upper() in FORM_LABELS:
        return None
    # Alignment proof: whatever follows the description must be a term or an
    # amount. If it is neither, we are not where we think we are in the form.
    tail = " ".join(values[2:5])
    if not (FORM_MONEY.search(tail) or FORM_TERM.search(tail)):
        return None
    return value


# The Department of Roads files a one-sentence notice in place of a document
# for items it bought outright. The whole PDF is this and nothing else, one
# wording exactly, 7,577 times, every one of them Roads:
#
#   "This item involved a direct purchase which did not result in a contract.
#    Therefore, there is no contract available for this item. Questions
#    regarding these transactions should be directed to the NDOR Communication
#    Division at 402-479-4512."
#
# These rows used to fall through to the page's "No description could be read
# from this document", which is true of our parsers and unfair to the state --
# it did answer, in writing, and its answer is better than our sentence. It is
# published whole, phone number included, on the same rule that keeps invoicing
# boilerplate in line items: which of the state's words are worth keeping is
# not our call.
#
# Matched at the start of the document rather than anywhere in it. The sentence
# also turns up quoted inside correspondence about such a purchase, where it
# describes some other item and not the one this row is about.
DIRECT_PURCHASE = re.compile(
    r"^\s*This item involved a direct purchase which did not result in a "
    r"contract\.", re.I)


def from_direct_purchase(text):
    """The state's own account of a purchase it made without a contract."""
    flat = flatten(text)
    if not DIRECT_PURCHASE.match(flat):
        return None
    return flat[:MAX_DESCRIPTION] or None


# The State Purchasing Bureau's award, amendment and purchase-order forms open
# their free-text block with a sentence saying what the state bought: "Contract
# to supply and deliver Fine Gradation Brining Salt to the State of Nebraska as
# per the attached specifications for the contract period November 14, 2022
# through November 13, 2023." It is the closest thing to a scope of work on the
# state agency side of the database, and it reaches 876 documents that no other
# pattern here describes.
#
# Anchored to the start of a line. Unanchored it also matches inside running
# contract prose ("...agreement to provide a certain number of shows..."),
# which would publish a fragment of one clause as the contract's scope.
# The Department of Transportation's own "Contract Description" field, off its
# change-order and award reports. A labelled field the state filled in, not
# prose to be interpreted -- the same kind of thing as the direct-purchase
# notice, and lifted the same way.
#
# The value runs until the next column of the report, which the text extraction
# renders as a run of spaces, or until one of the labels that follows it.
# The fields of the Department of Transportation's change-order report, named
# rather than guessed at. Two attempts to infer where a value ends both failed
# on real documents and are worth recording: stopping at a run of whitespace
# reads only the first line of a value the PDF wrapped ("I-80," for a location
# that continues), and stopping at "any run of capitalised words then a colon"
# fires inside the value itself, because "WEEPING WATER SPUR Contract
# Description:" fits that shape exactly and truncated the location to "S13K,".
#
# Enumerating is safe here only because from_contract_description refuses to
# run on any document that is not this report.
DOT_REPORT_LABELS = "|".join([
    "Special Notes", "Contract Description", "Change Order Approval Date",
    "Letting Date", "Change Order Type", "Change Order Nbr", "Change Order Report Date",
    "Primary Project Information", "Project Information",
    "Primary Project Location", "Project Location",
    "Zero Dollar Change Order", "Work Force Account ID", "Contract ID",
    "Potential for Design Error/Omission", "Vendor", "Page",
    "Prev Revised", "This Change", "Pct Change", "Funding Split",
    "Suppl Description", "CO Item Description", "Revised Total",
])
NEXT_LABEL = rf"(?=\s+(?:{DOT_REPORT_LABELS})\s*:|$)"

# A value that contains one of the form's own field names ran into a label
# rather than stopping at it, which happens where the report prints the field
# empty. Eleven documents in 4,080 did, and they produced "Change Order
# Approval Date — Contract Description" as a contract's scope. Cheap to detect
# and not worth trying to parse: a blank field is a blank description.
RAN_INTO_A_LABEL = re.compile(DOT_REPORT_LABELS, re.IGNORECASE)

# Matched against the text with its whitespace flattened, never the raw
# extraction, so a value the PDF wrapped is still read whole.
CONTRACT_DESCRIPTION = re.compile(
    r"Contract Description:\s*(.{3,200}?)" + NEXT_LABEL, re.IGNORECASE)

PROJECT_LOCATION = re.compile(
    r"Primary Project Location:\s*(.{3,120}?)" + NEXT_LABEL, re.IGNORECASE)

PURCHASING_BUREAU = re.compile(
    r"(?m)^[ \t]*((?:Contract|One Time Purchase|Purchase Order)\s+to\s+"
    r"(?:supply|provide|furnish)\b[^\n]*)")

# Where the sentence stops being about the purchase and starts being about
# process. Built the way FURNITURE was, by counting what actually follows the
# lead across every document that carries it rather than from a guess: the
# vendor's contact block accounts for 205 of them and the ACH enrolment notice
# for 148.
#
# Renewal and term sentences are deliberately not here. "This is the first
# renewal of the contract as amended" is the state describing this contract,
# not administrative furniture, and it survives into the description.
BUREAU_TAIL = re.compile(
    r"(?:"
    r"Vendor\s+(?:Point\s+of\s+)?Contact"
    r"|The State may request that payment"
    r"|The Contractor is required and hereby agrees"
    r"|found at:\s*<?http"
    r"|Payment(?:\s+Terms)?:\s"
    r"|IMPORTANT NOTE:"
    r"|A response to this Solicitation"
    r"|PLEASE READ CAREFULLY"
    r")", re.I)

# The form wraps its own lead and the text layer sometimes emits the wrapped
# half twice: "Contract to supply and deliver Contract to supply and deliver
# Armor Coat, Surfacing, Windrow and Deicing Gravel". Collapsing the repeat is
# not editing the state's words -- the doubling is an artifact of the text
# extraction, and the page would otherwise print a stutter the PDF does not
# have.
BUREAU_STUTTER = re.compile(
    r"^((?:Contract|One Time Purchase|Purchase Order)\s+to\s+"
    r"(?:supply|provide|furnish)(?:\s+and\s+deliver)?\s+)\1", re.I)

# How far past the lead line to keep reading. The sentence wraps across at most
# three continuation lines in every document measured; the cap is insurance
# against a text layer with no line ends, not a truncator.
BUREAU_CONTINUATION_LINES = 4


def from_purchasing_bureau(text):
    """The State Purchasing Bureau's "Contract to supply and deliver" sentence.

    Ranked last, so it only ever fills a blank. It was tried above line items
    first, on the strength of a purchase order that reads "GSA MODEL; FRIEGHT;
    HULL QUOTE ITEM F" off its table and "One Time Purchase to supply and
    deliver AirBoats to the State of Nebraska" here. Run across the whole
    corpus that ordering rewrote 671 existing descriptions and the trade went
    both ways: it buys readability with specifics. A boat whose items gave its
    length, beam, transom and deck became "to supply and deliver boat"; "SNOW
    GROOMER FOUR CYLINDER" became "SNOW GROOMER"; a Network Nebraska circuit
    lost the school it was for.

    The state writing a headline is not the state writing a better description,
    and choosing between two of its own phrasings is an editorial call this
    file has no business making. So: 879 rows that had nothing now have this,
    and nothing that already had a description changes.
    """
    match = PURCHASING_BUREAU.search(text)
    if not match:
        return None

    lines = [match.group(1)]
    for follower in text[match.end():].split("\n")[:BUREAU_CONTINUATION_LINES]:
        if not follower.strip():
            continue
        if BUREAU_TAIL.search(follower):
            break
        lines.append(follower.strip())

    value = flatten(" ".join(lines))
    tail = BUREAU_TAIL.search(value)
    if tail:
        value = value[:tail.start()].strip()
    value = BUREAU_STUTTER.sub(r"\1", value)

    # Long enough to name a thing. Anything shorter is the lead sentence with
    # its object on a line the text layer put somewhere else entirely.
    if len(value) < 30:
        return None
    return value[:MAX_DESCRIPTION]


def dedupe(candidates):
    """Distinct, in first-seen order. A purchase order repeats identical rows."""
    seen = set()
    items = []
    for candidate in candidates:
        item = flatten(candidate)
        if item and item not in seen:
            seen.add(item)
            items.append(item)
    return items


def university_items(flat, text=None):
    """University line items, with the description column's wrapped tail.

    The description column is 40 characters wide and the text *wraps* inside
    it rather than being cut, so reading only the line that carries the money
    columns truncates almost everything. Measured over 28,118 documents:
    54,280 items wrap and 3,780 do not, so the single-line reading was wrong
    93% of the time. It made a $15 M item read "GENERAL CONSTRUCTION SERVICES
    FOR" when the state wrote "GENERAL CONSTRUCTION SERVICES FOR REMODEL OF
    BOB DEVANTEY SPORTS CENTER PER UNL INVITATION TO BID 909353-12." (their
    spelling of Devaney, kept).

    The tail cannot simply be taken, because pdf text extraction does not emit
    the page in reading order: on many documents the lines after an item are
    the vendor address block and then the table header, which were never in
    the description column at all. FURNITURE is the empirically-built list of
    those, taken from the most common lines following an item across 40,000
    documents. Stop at one and the tail is whatever preceded it.

    Everything that is not furniture is kept verbatim, including the invoicing
    boilerplate ("*Please reference "Project 13781"...") and change-order logs.
    Those read as noise but somebody typed them into that field, and deciding
    which of the state's own words are worth keeping is not our call to make.
    """
    if text is None:
        return dedupe(UNIVERSITY_ITEM.findall(flat))

    lines = text.split("\n")
    descriptions = []
    for i, line in enumerate(lines):
        match = UNIVERSITY_LINE.match(line)
        if not match:
            continue
        parts = [match.group(1)]
        for follower in lines[i + 1:i + 1 + MAX_CONTINUATION_LINES]:
            if not follower.strip():
                continue
            if FURNITURE.match(follower) or UNIVERSITY_LINE.match(follower):
                break
            parts.append(follower.strip())
        descriptions.append(flatten(" ".join(parts)))

    # A few documents have a text layer whose lines have collapsed into one, so
    # nothing anchors to a line end. The flattened pattern still finds those.
    return dedupe(descriptions) or dedupe(UNIVERSITY_ITEM.findall(flat))


def state_items(flat):
    """Line items off the state's form, each with its wrapped tail reattached."""
    start = flat.find(STATE_TABLE)
    if start < 0:
        return []
    table = flat[start + len(STATE_TABLE):]

    rows = list(STATE_ITEM.finditer(table))
    end_of_table = table.find("Total Order")
    if end_of_table < 0:
        end_of_table = len(table)

    descriptions = []
    for i, row in enumerate(rows):
        # Everything up to the next row -- or the total -- trails this one.
        stop = rows[i + 1].start() if i + 1 < len(rows) else end_of_table
        tail = flatten(table[row.end():max(stop, row.end())])
        description = row.group(1)

        furniture = STATE_FURNITURE.search(tail)
        if furniture:
            tail = tail[:furniture.start()].strip()

        # A tail carrying row columns is the next line item, not this one's
        # continuation. Drop the whole thing rather than guessing where the row
        # begins: its own description sits in front of its numbers, so a wrong
        # cut would file one contract's words under another's money.
        if tail and not ROW_COLUMNS.search(tail) and len(tail) <= MAX_CONTINUATION:
            description += " " + tail
        descriptions.append(description)

    return dedupe(descriptions)


def from_services_clause(text):
    """The contract's own SERVICES clause, or None.

    For contract templates with no cover sheet and no line items -- the
    University's work-made-for-hire and professional-services agreements. The
    clause is the state's own words like everything else here, boilerplate
    included: "University hereby engages Copyeditor ... to copyedit the journal
    Studies in American Indian Literatures 36, numbers 1-2, edited by Kiara
    Vigil". The substance sits at the end of that sentence, and trimming the
    front would mean deciding which of the state's words count.

    Ranked below the cover sheet, which is a summary somebody wrote on purpose,
    and above line items, which these documents do not have anyway.
    """
    match = SERVICES_CLAUSE.search(flatten(text))
    if not match:
        return None
    value = flatten(match.group(1))
    return value[:MAX_DESCRIPTION] or None


def from_line_items(text):
    """Every distinct line-item description, in the order they appear.

    Which form this is gets decided before either parser runs, rather than by
    letting whichever pattern matches first win. That ordering was a latent
    trap: widening the University unit-price column to accept four decimals
    made its row pattern start matching lines inside *state* food-service
    orders, which then never reached the state parser at all. A 1,000-character
    grocery order came back as "26 CHICKEN BREAST BONELESS 48/4OZ 1.0000 CS".

    The state's table header is the discriminator. Measured over 1,025 state
    documents it never once co-occurs with a University row.
    """
    flat = flatten(text)
    if STATE_TABLE in flat:
        return state_items(flat) or university_items(flat, text)
    return university_items(flat, text) or state_items(flat)


def from_contract_description(text):
    """The Department of Transportation's own "Contract Description" field.

    Ranked last with from_purchasing_bureau, so it only ever fills a blank and
    nothing already described changes. The location field printed beside it is
    joined on, because the work shorthand alone never says where.

    What it returns is highway shorthand -- "GRAD CONC PAVE CULV SEED BR GDRL
    FENCE ELEC SIGN" is grading, concrete paving, culverts, seeding, bridge,
    guardrail, fence, electrical and signs. Terse, and jargon, and the state's
    own words for what the contract covers, which is the standard everything
    else here is held to. Measured over the corpus: 2,371 documents carry it,
    median 31 characters, a tenth of them under twelve.

    Those short ones are the reason for the length floor. A field reading
    "MISC" satisfies "this row has a description" while telling a reader
    nothing, and a blank at least says plainly that we could not describe it.
    Four characters is not much of a bar, but it is the one that keeps the
    emptiest of them out.

    Two neighbouring labels were tried and rejected, both for the same reason
    and it is worth recording so nobody re-tries them: "Change Order
    Description:" appears on 1,294 documents with the field left empty, so
    anything after it is the *next* field -- "DocuSign Envelope ID:
    FA5BA6D3-..." became a contract's description in testing. "Scope of Work"
    matched 113 and captured the middle of a sentence. Both would have raised
    the parse rate and lowered the accuracy, which this project has already
    paid for once.
    """
    # Confined to the report this was built and checked against, which is
    # identified by the location field printed beside the description. Other
    # forms use the same label for something else entirely -- a HIPAA business
    # associate agreement fills it with the obligations it imposes, wrapped
    # over several lines, and this parser stops at the first of them. Reading
    # only the first line of a wrapped field is the exact bug that truncated
    # 93% of the University purchase-order descriptions, so a form nobody has
    # verified this against does not get parsed at all.
    flat = " ".join(text.split())
    where = PROJECT_LOCATION.search(flat)
    if not where:
        return None

    match = CONTRACT_DESCRIPTION.search(flat)
    if not match:
        return None
    value = " ".join(match.group(1).split()).strip(" :-")
    if len(value) < 4 or RAN_INTO_A_LABEL.search(value):
        return None

    # Both are the state's own labelled fields off the same report, and the
    # work field alone is close to unreadable without the place: "GRAD SEED BR
    # GDRL BIT" tells you a road was graded, seeded, bridged, guardrailed and
    # surfaced, but not which road. Joined with an em dash because the
    # locations are full of hyphens themselves -- "US-275, N-64 - L28B".
    place = " ".join(where.group(1).split()).strip(" :-")
    if RAN_INTO_A_LABEL.search(place):
        place = ""
    if len(place) >= 3 and place.lower() not in value.lower():
        value = f"{value} \u2014 {place}"
    return value


def describe(text):
    """(source, description, items) for one document, or None if it says nothing.

    Cover sheet wins where both exist: a purchase order attached to a contract
    carries both, and the sentence a person wrote beats a list of part numbers.

    The direct-purchase notice is tried first and not because it tells you the
    most -- it tells you the least. Those documents contain that sentence and
    nothing else, so nothing below it could fire anyway, and matching it up
    front keeps the reason a row is bare out of the parsers that describe work.
    """
    notice = from_direct_purchase(text)
    if notice:
        return "direct_purchase", notice, []

    summary = from_cover_sheet(text)
    if summary:
        return "cover_sheet", summary, []

    form = from_cover_sheet_form(text)
    if form:
        return "cover_sheet_form", form, []

    clause = from_services_clause(text)
    if clause:
        return "services_clause", clause, []

    items = from_line_items(text)
    if items:
        return "line_items", "; ".join(items), items

    bureau = from_purchasing_bureau(text)
    if bureau:
        return "purchasing_bureau", bureau, []

    highway = from_contract_description(text)
    if highway:
        return "contract_description", highway, []

    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=OUT_JSONL, help=f"output path (default {OUT_JSONL})")
    parser.add_argument("--sample", type=int, metavar="N",
                        help="print N random results per source and exit without writing")
    args = parser.parse_args()

    if not os.path.exists(IN_JSONL):
        raise SystemExit(f"missing {IN_JSONL} — run scripts/extract_text.py first")

    status = collections.Counter()      # what the text capture found
    found = collections.Counter()       # what this script could describe
    lengths = collections.defaultdict(list)
    truncated = 0
    samples = collections.defaultdict(list)
    records = []

    current = current_lines(IN_JSONL)
    superseded = 0

    with open(IN_JSONL, encoding="utf-8") as f:
        for number, line in enumerate(f):
            if number not in current:
                superseded += 1
                continue
            entry = json.loads(line)
            data = entry["data"]
            status[data["status"]] += 1
            if data["status"] != "text":
                continue

            result = describe(data["text"])
            if result is None:
                found["no description found"] += 1
                continue

            source, description, items = result
            if len(description) > MAX_DESCRIPTION:
                description = description[:MAX_DESCRIPTION]
                items = []
                truncated += 1

            found[source] += 1
            lengths[source].append(len(description))
            samples[source].append((data["doc"], description))
            records.append({
                "tok": entry["tok"],
                "doc": data["doc"],
                "source": source,
                "description": description,
                "items": items,
            })

    if args.sample:
        rng = random.Random(0)
        for source in sorted(samples):
            print(f"\n=== {source} — {len(samples[source]):,} documents "
                  f"({args.sample} at random) ===\n")
            for doc, description in rng.sample(samples[source],
                                               min(args.sample, len(samples[source]))):
                print(f"  {doc}")
                print(f"      {description[:300]}\n")
        return

    with open(args.out, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    readable = status["text"]
    print(f"documents with captured text : {readable:,} of {sum(status.values()):,}")
    for name, count in status.most_common():
        if name != "text":
            print(f"  {name:<12} {count:>7,}  (nothing to read)")

    print(f"\ndescribed                    : {len(records):,} "
          f"({100 * len(records) / max(readable, 1):.1f}% of readable documents)")
    for source in sorted(lengths):
        sizes = sorted(lengths[source])
        print(f"  {source:<12} {found[source]:>7,}   median {sizes[len(sizes) // 2]} chars, "
              f"longest {sizes[-1]}")
    if found["no description found"]:
        print(f"  {'(none)':<12} {found['no description found']:>7,}   "
              "readable, but neither pattern matched")
    if superseded:
        print(f"\n{superseded:,} superseded log entries skipped (documents re-fetched later)")
    if truncated:
        print(f"\n{truncated} description(s) hit the {MAX_DESCRIPTION}-char cap — "
              "either a document with very many line items, or a layout whose end "
              "this script fails to find. Read them before trusting them.")

    print(f"\nwritten to {args.out}")
    print("read some: python3 scripts/extract_scope.py --sample 20")


if __name__ == "__main__":
    main()
