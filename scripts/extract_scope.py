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

# A purchase-order line: sequence number, quantity, unit, description, unit
# price, extended price. Anchoring on the two trailing money columns is what
# makes the description boundary unambiguous.
#
# Verified against 7,375 University of Nebraska purchase orders. State agencies
# use a different form ("State of Nebraska Purchase Order", a Line/Description/
# Quantity table) that is not yet represented in the captured text -- when that
# text exists, add its pattern here rather than loosening this one.
LINE_ITEM = re.compile(
    r"\b\d{3}\s+\d[\d,]*\s+[A-Z]{2,4}\s+(.{3,120}?)\s+[\d,]+\.\d\d\s+[\d,]+\.\d\d"
)

# Long enough for a paragraph someone genuinely typed, short enough that a
# runaway capture is obvious rather than shipped. Nine documents in the current
# text exceed the old boundary set; this is the backstop if a third layout
# turns up.
MAX_DESCRIPTION = 4000


def flatten(s):
    return " ".join(s.split())


def from_cover_sheet(text):
    """The hand-written contract summary, or None."""
    if "Contract Summary" not in text:
        return None
    match = COVER_SHEET.search(text)
    if not match:
        return None
    value = flatten(match.group(1))
    return value or None


def from_line_items(text):
    """Every distinct line-item description, in the order they appear."""
    seen = set()
    items = []
    for raw in LINE_ITEM.findall(flatten(text)):
        item = flatten(raw)
        if item and item not in seen:
            seen.add(item)
            items.append(item)
    return items


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

    with open(IN_JSONL, encoding="utf-8") as f:
        for line in f:
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
    if truncated:
        print(f"\n{truncated} description(s) hit the {MAX_DESCRIPTION}-char cap — "
              "likely a cover-sheet layout this script does not close correctly")

    print(f"\nwritten to {args.out}")
    print("read some: python3 scripts/extract_scope.py --sample 20")


if __name__ == "__main__":
    main()
