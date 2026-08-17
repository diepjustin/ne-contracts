"""Recording what changed between one daily scrape and the next.

The database was a photograph: every night's scrape overwrote what it knew and
kept no memory of movement. Document 45500 at the Medical Center reads
$2,558,983 expired and $21,204,743 active -- the same contract, amended -- and
nothing in the data could show it had moved.

These tests pin the two halves that matter: a change is recorded exactly once
with both values, and an unchanged record writes nothing at all. The second is
as important as the first, because --daily runs against every Active record
every night and a false positive would bury the real ones.
"""

import json
import os
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import scrape  # noqa: E402


HEADER = ["Document Number", "Document Type", "Entity Code", "Entity Name", "Vendor",
          "Amount", "Begin Date", "End Date", "Status", "Detail URL", "View URL"]


def write_csv(path, rows):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)


def a_row(**over):
    row = ["45500", "CN", "051", "University of Nebraska Medical Center",
           "MCCARTHY BUILDING COMPANIES", "$2,558,983.00", "03/01/2020", "12/31/2026",
           "Active", "https://example.test/detail?D=abc", ""]
    for key, value in over.items():
        row[HEADER.index(key)] = value
    return row


def a_record(**over):
    record = {
        "doc_number": "45500", "doc_type": "CN", "entity_code": "051",
        "entity_name": "University of Nebraska Medical Center",
        "vendor": "MCCARTHY BUILDING COMPANIES", "amount": "$2,558,983.00",
        "begin_date": "03/01/2020", "end_date": "12/31/2026",
        "detail_url": "https://example.test/detail?D=abc",
    }
    record.update(over)
    return record


def read_changes(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_baseline_carries_the_watched_fields_for_active_rows(tmp_path):
    csv_path = str(tmp_path / "d.csv")
    write_csv(csv_path, [a_row(), a_row(**{"Status": "Expired", "Detail URL": "https://example.test/x"})])
    known, baseline = scrape.load_known_active(csv_path)

    assert known == {"University of Nebraska Medical Center": {"https://example.test/detail?D=abc"}}
    # Expired rows are not re-scanned by --daily, so carrying them would cost
    # memory for a comparison that never happens.
    assert set(baseline) == {"https://example.test/detail?D=abc"}


def test_a_changed_amount_is_recorded_with_both_values(tmp_path):
    csv_path, out = str(tmp_path / "d.csv"), str(tmp_path / "changes.jsonl")
    write_csv(csv_path, [a_row()])
    _known, baseline = scrape.load_known_active(csv_path)

    assert scrape.record_changes(a_record(amount="$21,204,743.00"), baseline, out) == 1
    changes = read_changes(out)
    assert len(changes) == 1
    assert changes[0]["field"] == "amount"
    assert changes[0]["from"] == "$2,558,983.00"
    assert changes[0]["to"] == "$21,204,743.00"
    assert changes[0]["doc"] == "45500"


def test_an_unchanged_record_writes_nothing(tmp_path):
    """--daily sees every Active record every night. A false positive here
    would bury the real amendments under a nightly re-record of everything."""
    csv_path, out = str(tmp_path / "d.csv"), str(tmp_path / "changes.jsonl")
    write_csv(csv_path, [a_row()])
    _known, baseline = scrape.load_known_active(csv_path)

    assert scrape.record_changes(a_record(), baseline, out) == 0
    assert read_changes(out) == []


def test_several_fields_moving_are_recorded_separately(tmp_path):
    csv_path, out = str(tmp_path / "d.csv"), str(tmp_path / "changes.jsonl")
    write_csv(csv_path, [a_row()])
    _known, baseline = scrape.load_known_active(csv_path)

    scrape.record_changes(a_record(amount="$3.00", end_date="12/31/2030"), baseline, out)
    assert {c["field"] for c in read_changes(out)} == {"amount", "end date"}


def test_a_record_with_no_baseline_is_not_a_change(tmp_path):
    """A record the CSV has never seen is new, not amended -- --daily writes it
    as a row, and calling that a change would report every new contract as an
    amendment to nothing."""
    csv_path, out = str(tmp_path / "d.csv"), str(tmp_path / "changes.jsonl")
    write_csv(csv_path, [a_row()])
    _known, baseline = scrape.load_known_active(csv_path)

    assert scrape.record_changes(a_record(**{"detail_url": "https://example.test/new"}),
                                 baseline, out) == 0
    assert read_changes(out) == []


def test_the_log_is_append_only_and_folds_on_key(tmp_path):
    """Same shape as doc_text.jsonl: a field that moves twice appears twice,
    and the last entry wins. Counting lines would say the contract changed
    twice as much as it did."""
    csv_path, out = str(tmp_path / "d.csv"), str(tmp_path / "changes.jsonl")
    write_csv(csv_path, [a_row()])
    _known, baseline = scrape.load_known_active(csv_path)

    scrape.record_changes(a_record(amount="$10.00"), baseline, out)
    scrape.record_changes(a_record(amount="$20.00"), baseline, out)

    entries = read_changes(out)
    assert len(entries) == 2
    latest = {}
    for entry in entries:
        latest[entry["key"]] = entry
    assert len(latest) == 1
    assert list(latest.values())[0]["to"] == "$20.00"
