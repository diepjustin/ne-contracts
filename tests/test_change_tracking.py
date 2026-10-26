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

import csv
import json
import os
import sys

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.join(os.path.dirname(TESTS), "scripts"))

import scrape  # noqa: E402
from test_status_flips import (CAMPUS, OTHER, RESTORED_VIEW, listed, night,  # noqa: E402
                               row, stamped)


HEADER = ["Document Number", "Document Type", "Entity Code", "Entity Name", "Vendor",
          "Amount", "Begin Date", "End Date", "Status", "Detail URL", "View URL"]


def write_csv(path, rows):
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


# --- amended rows are rewritten, so a change is logged once (audit n3, 26 Oct 2026) ---------
#
# Until Oct 2026 --daily logged an amendment and left the row alone: the site kept the old
# amount, and the baseline never moved, so the same change was logged again every night.

def changes_of(tmp_path):
    return read_changes(str(tmp_path / "data" / "changes.jsonl"))


def the_row(out, n):
    (found,) = [r for r in out if r[0] == str(n)]
    return found


def test_an_amended_row_takes_tonights_values_and_is_logged_once(monkeypatch, tmp_path):
    rows = [row(n) for n in range(5)]
    search = {CAMPUS: [listed(n) for n in range(4)] + [listed(4, amount="$250.00")]}
    out = night(monkeypatch, tmp_path, rows, search, entities=(CAMPUS,))

    amended = the_row(out, 4)
    assert amended[5] == "$250.00" and amended[8] == "Active"
    assert amended[3] == CAMPUS, "the canonical entity name, not the grid's capitals"
    assert amended[10] == RESTORED_VIEW, "its detail page is read again for tonight's documents"
    assert [e["k"] for e in night.documents] == [scrape.document_key(amended[9])]
    [change] = changes_of(tmp_path)
    assert (change["field"], change["from"], change["to"]) == ("amount", "$100.00", "$250.00")
    assert len(out) == 5, "rewritten in place, not appended"

    night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,))
    assert len(changes_of(tmp_path)) == 1, "the second night sees nothing new to log"
    assert stamped(tmp_path)


def test_an_amendment_waits_when_its_detail_page_cannot_be_read(monkeypatch, tmp_path):
    rows = [row(n) for n in range(5)]
    search = {CAMPUS: [listed(n) for n in range(4)] + [listed(4, amount="$250.00")]}
    out = night(monkeypatch, tmp_path, rows, search, entities=(CAMPUS,),
                documents={listed(4)["detail_url"]: None})
    assert the_row(out, 4)[5] == "$100.00"
    assert changes_of(tmp_path) == [], "logged the night its row is rewritten, not before"

    out = night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,))
    assert the_row(out, 4)[5] == "$250.00"
    assert len(changes_of(tmp_path)) == 1


def test_an_amendment_in_a_refused_entity_waits_for_a_night_that_is_judged(monkeypatch, tmp_path):
    """Most of CAMPUS missing is refused; whatever it did list is read again next run."""
    rows = [row(n) for n in range(30)] + [row(99, OTHER)]
    out = night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(0, amount="$250.00")],
                                              OTHER: [listed(99, OTHER)]})
    assert the_row(out, 0)[5] == "$100.00"
    assert changes_of(tmp_path) == []


def test_at_most_amend_limit_rows_are_rewritten_a_night(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "AMEND_LIMIT", 2)
    rows = [row(n) for n in range(5)]
    search = {CAMPUS: [listed(n, amount="$250.00") for n in range(5)]}
    out = night(monkeypatch, tmp_path, rows, search, entities=(CAMPUS,))
    assert sorted(r[5] for r in out) == ["$100.00"] * 3 + ["$250.00"] * 2
    assert "3 more amended record(s) wait for the next run" in capsys.readouterr().out

    out = night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,))
    out = night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,))
    assert all(r[5] == "$250.00" for r in out)
    assert len(changes_of(tmp_path)) == 5


@pytest.mark.parametrize("twins", [1, 2])
def test_the_rewrite_drops_rows_identical_to_one_above(tmp_path, twins):
    path = str(tmp_path / "d.csv")
    keep = a_row(Status="Expired")
    other = a_row(**{"Detail URL": "https://example.test/other"})
    write_csv(path, [keep, other] + [list(keep)] * twins)

    result = scrape.patch_statuses(path, {other[9]}, set())
    assert (result.expired, result.duplicates) == (1, twins)
    with open(path, newline="", encoding="utf-8") as f:
        body = list(csv.reader(f))[1:]
    assert body == [keep, other[:8] + ["Expired"] + other[9:]]


def test_a_near_twin_is_not_a_duplicate(tmp_path):
    """45500 at the Medical Center: the same Detail URL at two amounts is two rows."""
    path = str(tmp_path / "d.csv")
    old = a_row(Status="Expired")
    write_csv(path, [old, a_row(Amount="$21,204,743.00")])
    assert scrape.patch_statuses(path, {old[9]}, set()).duplicates == 0
