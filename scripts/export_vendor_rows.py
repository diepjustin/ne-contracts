"""Export per-vendor lazy JSON shards for ../ne-connect/ to fetch.

Deliberately separate from build_site.py: that script owns this project's
real full-text search index (a bespoke binary format -- token files, posting
lists, its own verification and selftest machinery), and nothing here reads
or writes any of it. ne-connect's own itemized-detail views for campaign
finance, lobbying, and disclosures each work the same way -- a small,
independent script reading the source's already-processed CSVs and writing
flat JSON keyed by the name ne-connect's own Party objects use -- and this
is the fourth. See ne-connect/docs/SCHEMA.md's "Cross-fetch" sections for
the pattern.

Sharded, not one file: a single vendor (Amazon Capital Services, ~66,900
purchase-order line items) alone makes a combined export ~190 MB -- past
GitHub's hard 100 MB per-file push limit, confirmed the hard way on a real
build. d/rows/<A-Z or _>.json shards by the vendor name's first character
instead of truncating any real record.

Usage:
    python scripts/export_vendor_rows.py
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONTRACT_FILES = ("nu_contracts.csv", "nu_purchase_orders.csv")
# One vendor alone (Amazon Capital Services, ~66,900 purchase-order line
# items) makes a single combined export ~190 MB -- past GitHub's hard
# 100 MB per-file push limit, confirmed the hard way on a real build.
# Sharded by the vendor name's first character instead of truncating any
# real record: 36 shards, largest ~42 MB (well under even the 50 MB warning
# threshold), all real data intact. ne-connect fetches only the one shard a
# looked-up vendor's name falls into.
OUT_DIR = ROOT / "d" / "rows"

# Some fields (Detail URL especially) can run long in practice -- match the
# same guard the other sibling repos already carry for the same reason.
csv.field_size_limit(sys.maxsize)


def to_amount(raw: str) -> float:
    try:
        return round(float((raw or "").replace("$", "").replace(",", "").strip()), 2)
    except ValueError:
        return 0.0


def shard_key(vendor: str) -> str:
    """First character of a vendor name, uppercased, A-Z bucketed together;
    anything else (a digit, punctuation, an empty string) falls into "_" so
    every vendor has exactly one shard file. ne-connect's JS computes this
    same key to pick which shard to fetch -- keep the two in sync.
    """
    first = (vendor or "").strip()[:1].upper()
    return first if "A" <= first <= "Z" else "_"


def build_vendor_rows() -> dict:
    """Vendor (raw, exact string) -> every contract/PO row naming it.

    Row shape: [document_number, document_type, entity_name, amount,
    begin_date, end_date, status, detail_url]. Keyed by the exact raw
    "Vendor" string, the same key ne-connect/ingest/sources.py
    load_contract_vendors() uses for its own Party objects -- no
    alias-guessing needed on the ne-connect side, same as lobbying/
    disclosures' id-based joins.
    """
    by_vendor = defaultdict(list)
    for filename in CONTRACT_FILES:
        path = DATA_DIR / filename
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                vendor = (row.get("Vendor") or "").strip()
                if not vendor:
                    continue
                by_vendor[vendor].append(
                    [
                        (row.get("Document Number") or "").strip(),
                        (row.get("Document Type") or "").strip(),
                        (row.get("Entity Name") or "").strip(),
                        to_amount(row.get("Amount")),
                        (row.get("Begin Date") or "").strip(),
                        (row.get("End Date") or "").strip(),
                        (row.get("Status") or "").strip(),
                        (row.get("Detail URL") or "").strip(),
                    ]
                )
    return dict(by_vendor)


def main() -> int:
    rows = build_vendor_rows()
    if not rows:
        print("no nu_contracts.csv/nu_purchase_orders.csv found under data/ -- run scripts/scrape.py first")
        return 1

    shards = defaultdict(dict)
    for vendor, vendor_rows in rows.items():
        shards[shard_key(vendor)][vendor] = vendor_rows

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Clear stale shards from a previous run (a key that had vendors before
    # but none now would otherwise keep serving old data forever).
    for existing in OUT_DIR.glob("*.json"):
        existing.unlink()

    total_bytes = 0
    largest = ("", 0)
    for key, vendor_rows in shards.items():
        payload = json.dumps(vendor_rows, separators=(",", ":"))
        (OUT_DIR / f"{key}.json").write_text(payload, encoding="utf-8")
        total_bytes += len(payload)
        if len(payload) > largest[1]:
            largest = (key, len(payload))

    print(f"  {len(rows):,} vendors, {len(shards)} shards  ->  d/rows/*.json"
          f" ({total_bytes / 1024 / 1024:.1f} MB total)")
    print(f"  largest shard: {largest[0]}.json ({largest[1] / 1024 / 1024:.1f} MB)")
    if largest[1] > 90 * 1024 * 1024:
        print("  WARNING: a shard is within 10 MB of GitHub's 100 MB hard push "
              "limit -- shard scheme needs revisiting.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
