"""Pull the description the state already wrote out of the documents it publishes.

The database exposes no description field anywhere -- a row is a document
number, a vendor, an amount and two dates. What a contract is actually *for*
exists only inside the PDF. But for most records it is already written there
in plain English, by the person who filed the document, and can be lifted
verbatim. Nothing here is generated or summarized; every string this script
emits is the state's own text.

Two sources, in descending order of how much they tell you:

  * A **cover sheet**. University of Nebraska contracts carry a Procure-to-Pay
    page whose "Contract Summary (brief description and/or event name)" field
    is filled in by hand: "Coaches Replay system to be used by MBB." This is
    the closest thing to a scope of work the state publishes.

  * A **line-item table**. Purchase orders list what was bought. The
    description column is a fixed 40-character field in the source form, so
    entries arrive truncated mid-word ("Logitech MK540 Advanced Wireless
    Keyboar"). That truncation is the state's, not ours, and it is why these
    are item descriptions rather than scopes of work.

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

# Purchase orders come on two different forms and neither pattern matches the
# other's layout. Anchoring on the trailing money columns is what makes the
# description boundary unambiguous in both.

# University: zero-padded line number, whole-number quantity, an alphabetic
# unit code, two-decimal money. The description column is a fixed 40 characters
# wide, which is where the mid-word truncation comes from.
UNIVERSITY_ITEM = re.compile(
    r"\b\d{3}\s+\d[\d,]*\s+[A-Z]{2,4}\s+(.{3,120}?)\s+[\d,]+\.\d\d\s+[\d,]+\.\d\d"
)

# The same row, anchored to a whole line, so the lines that follow can be read
# as the description column continuing. Used in preference to the pattern above
# wherever the text layer still has its line breaks.
UNIVERSITY_LINE = re.compile(
    r"^\s*\d{3}\s+\d[\d,]*\s+[A-Z]{2,4}\s+(.{3,120}?)\s+[\d,]+\.\d\d\s+[\d,]+\.\d\d\s*$"
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
# ("LABOR FOR BUILDING 14'X20'" ... "STORAGE GARAGE"). Only adopt that tail when
# it is short and carries no digits: anything numeric is the next row starting,
# not a continuation.
MAX_CONTINUATION = 80

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
        if tail and len(tail) <= MAX_CONTINUATION and not any(c.isdigit() for c in tail):
            description += " " + tail
        descriptions.append(description)

    return dedupe(descriptions)


def from_line_items(text):
    """Every distinct line-item description, in the order they appear."""
    flat = flatten(text)
    return university_items(flat, text) or state_items(flat)


def describe(text):
    """(source, description, items) for one document, or None if it says nothing.

    Cover sheet wins where both exist: a purchase order attached to a contract
    carries both, and the sentence a person wrote beats a list of part numbers.
    """
    summary = from_cover_sheet(text)
    if summary:
        return "cover_sheet", summary, []

    items = from_line_items(text)
    if items:
        return "line_items", "; ".join(items), items

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
