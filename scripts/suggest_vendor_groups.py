"""Propose vendor spellings that look like one company. Decide nothing.

The state records the same firm many ways -- legal suffixes come and go, a
contract number gets appended, punctuation drifts. Measured across all 738,195
rows: 26 spellings of Hausmann Construction total $1.40 B, where the site shows
$903 M under the largest single name. Kiewit is short $222 M across 4
spellings, Hawkins $74 M across 3. About 9,000 vendor strings sit in a cluster
with at least one other. So the database currently gives a wrong answer to the
most obvious question a reporter brings to it: how much has Nebraska paid this
company?

Fixing that automatically would be worse than leaving it broken. A wrong merge
invents spending that never happened -- two genuinely different firms with
similar names become one line in a story. So this script *only proposes*.
Nothing it prints changes the site. A grouping exists when, and only when,
somebody has written it into scripts/vendor_groups.json by hand, which is the
same arrangement type_groups.json uses for document codes.

The money is extremely concentrated, so reviewing by spend descending means a
few dozen decisions cover most of the error. Read the top of this report, move
what is genuinely one company into vendor_groups.json, and ignore the tail.

    python3 scripts/suggest_vendor_groups.py            # top 40 by spend
    python3 scripts/suggest_vendor_groups.py --top 200
    python3 scripts/suggest_vendor_groups.py --json     # machine-readable

Every candidate needs a human to look at it. Two of the traps this normalisation
walks into on purpose, so you can see them rather than not:

  * A trailing number is usually a contract reference ("BOCKMANN, INC. 14721"),
    but it is sometimes part of the name -- a numbered franchise or district.
  * Stripping suffixes merges "SMITH CONSTRUCTION LLC" with "SMITH
    CONSTRUCTION INC", which is normally right and occasionally two unrelated
    firms sharing a family name.
"""

import argparse
import collections
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ne_format  # noqa: E402

GROUPS_JSON = os.path.join(ROOT, "scripts", "vendor_groups.json")

# Legal suffixes and connectives that differ between recordings of one firm.
NOISE = re.compile(r"\b(INC|INCORPORATED|LLC|L L C|LLP|LTD|CO|CORP|CORPORATION|"
                   r"COMPANY|COMPANIES|THE|OF|AND|DBA|PC|PLLC|LP)\b")

# A trailing run of digits is nearly always a contract number the state has
# appended to the name rather than part of it.
TRAILING_NUMBER = re.compile(r"[\s.,#-]*\d[\d\s.-]*$")


def normalise(name):
    """A crude key for 'probably the same company'. Deliberately not clever."""
    key = name.upper()
    key = TRAILING_NUMBER.sub(" ", key)
    key = NOISE.sub(" ", key)
    key = re.sub(r"[^A-Z0-9 ]", " ", key)
    return " ".join(key.split())


def load_reviewed():
    """Spellings already decided, so the report stops proposing them."""
    if not os.path.exists(GROUPS_JSON):
        return {}
    with open(GROUPS_JSON, encoding="utf-8") as f:
        groups = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    return {spelling: canonical for canonical, members in groups.items()
            for spelling in members}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--top", type=int, default=40,
                        help="how many candidate groups to report (default 40)")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON shaped like vendor_groups.json, to edit down by hand")
    parser.add_argument("--build", default=None,
                        help="payload directory (default: whatever manifest.json points at)")
    args = parser.parse_args()

    outdir = args.build
    if not outdir:
        with open(os.path.join(ROOT, "manifest.json"), encoding="utf-8") as f:
            outdir = os.path.join(ROOT, json.load(f)["dir"])

    columns, _docs, vendors, _vtok, meta = ne_format.read_payload(outdir)
    names = [v.decode("utf-8") for v in vendors]

    spend = collections.Counter()
    records = collections.Counter()
    for row in range(meta["count"]):
        vendor = columns["vendorIdx"][row]
        spend[vendor] += columns["amount"][row]
        records[vendor] += 1

    clusters = collections.defaultdict(list)
    for index, name in enumerate(names):
        key = normalise(name)
        if key:
            clusters[key].append(index)

    reviewed = load_reviewed()
    candidates = []
    for key, members in clusters.items():
        if len(members) < 2:
            continue
        if all(names[i] in reviewed for i in members):
            continue                      # already decided; do not propose again
        members.sort(key=lambda i: -spend[i])
        candidates.append((sum(spend[i] for i in members), key, members))
    candidates.sort(reverse=True)

    if args.json:
        # Shaped like vendor_groups.json so it can be edited down rather than
        # retyped. Canonical name defaults to the highest-spending spelling,
        # which is a starting point and not a decision.
        print(json.dumps(
            {names[members[0]]: [names[i] for i in members]
             for _total, _key, members in candidates[:args.top]},
            indent=2, ensure_ascii=False))
        return

    total_at_stake = sum(t for t, _, _ in candidates)
    print(f"{len(names):,} vendor strings, {len(candidates):,} unreviewed clusters "
          f"of 2 or more")
    print(f"${total_at_stake / 1e9:,.2f} B of spending sits in them\n")
    print(f"Top {min(args.top, len(candidates))} by total spend. Move the ones that are "
          f"genuinely one company into\nscripts/vendor_groups.json; leave anything you "
          f"are unsure about alone.\n")

    for total, _key, members in candidates[:args.top]:
        biggest = spend[members[0]]
        print(f"  ${total / 1e6:>10,.1f} M total   {len(members)} spellings   "
              f"(site shows ${biggest / 1e6:,.1f} M)")
        for i in members:
            print(f"      ${spend[i] / 1e6:>9,.1f} M  {records[i]:>5} rec  {names[i]}")
        print()


if __name__ == "__main__":
    main()
