"""Deciding a record's Status from one night's Active search.

On 17-18 Aug 2026 the state's search returned "No results found" for every
entity for two days. `scrape_entity` treats an empty first page as a clean
finish, so `--daily` saw zero active records, computed `known - seen` as
everything, and marked all 44,063 active records in the database Expired in a
single run. Every step succeeded; the run was green. It could not repair itself
either -- `--daily` only ever flips Active to Expired, so the damage was
permanent until the data was rebuilt by hand.

The refusal added for that had no floor, and it froze the other way. From 25
Sep 2026 an agency with one active record that genuinely ended was refused
every night, the state dataset never finished, its scrape time stuck at 24 Sep,
and the flips patched on those nights reached no report the guard rail reads.

These tests pin that a flip is evidence of an ending rather than of an absence,
that ordinary and small endings still happen, and that every patched flip
reaches the guard rail.
"""

import argparse
import csv
import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import check_daily_diff  # noqa: E402
import scrape  # noqa: E402


def urls(*names):
    return {f"https://example.test/detail?D={n}" for n in names}


# --- the refusal itself -----------------------------------------------------

def test_an_empty_answer_never_expires_anything():
    """The 17 Aug shape: the entity had active contracts and returned nothing."""
    assert scrape.refuse_flips(urls(*range(50)), set())


def test_a_wholesale_disappearance_is_refused():
    """A partial outage: most records missing at once is not 90 endings."""
    assert scrape.refuse_flips(urls(*range(100)), urls(*range(10)))


def test_an_ordinary_expiry_still_flips():
    """The guard must not freeze the database: a few contracts ending is normal
    and is the entire point of --daily."""
    assert scrape.refuse_flips(urls(*range(100)), urls(*range(3, 100))) is None


def test_a_small_entity_losing_most_of_its_records_is_still_allowed():
    assert scrape.refuse_flips(urls(*range(4)), urls(0)) is None


@pytest.mark.parametrize("active", [1, 4])
def test_a_small_entity_losing_everything_is_still_allowed(active):
    """Below the floor, a genuine clear-out must not be blocked -- a body with
    four contracts can legitimately end all four, and one with a single
    contract ending it is what froze the state dataset for four nights."""
    assert scrape.refuse_flips(urls(*range(active)), set()) is None


def test_five_active_records_all_gone_is_refused():
    assert scrape.refuse_flips(urls(*range(5)), set())


def test_nothing_known_means_nothing_to_flip():
    assert scrape.refuse_flips(set(), set()) is None


def test_the_floor_is_the_guard_rails_baseline():
    """Raised alone, the scrape would flip small clear-outs the guard rail then
    fails the night for; lowered alone, a small body's last contract ending
    would freeze the dataset again."""
    assert scrape.ALL_GONE_FLOOR == check_daily_diff.MIN_BASELINE


# --- whole nights, against a scripted Active search ---------------------------

CAMPUS = "Chadron State College"
OTHER = "Peru State College"
OLD_VIEW = "https://example.test/view?D=first"
NEW_VIEW = "https://example.test/view?D=fetched-tonight"


def row(n, entity=CAMPUS, status="Active", amount="$100.00"):
    return [str(n), "Contract", "050", entity, "ACME SUPPLY", amount, "07/01/2026",
            "06/30/2027", status, f"https://example.test/detail?D={n}", OLD_VIEW]


def listed(n, entity=CAMPUS, amount="$100.00"):
    """A record as the search grid reports it -- entity name shouted, as state
    grids do, while the CSV holds the canonical name."""
    return {"doc_number": str(n), "doc_type": "Contract", "entity_code": "050",
            "entity_name": entity.upper(), "vendor": "ACME SUPPLY", "amount": amount,
            "begin_date": "07/01/2026", "end_date": "06/30/2027",
            "detail_url": f"https://example.test/detail?D={n}"}


def night(monkeypatch, tmp_path, rows, search, entities=(CAMPUS, OTHER)):
    """One `scrape.py contract --daily` over `rows`, the search answering `search`.

    The fake honours scrape_entity's contract, including asking record_filter
    twice per record -- once to choose what to fetch, once before writing.
    Rows are only written on the first call; later calls reuse the CSV.
    """
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    csv_path = data / "nu_contracts.csv"
    if rows is not None:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([scrape.CSV_HEADER] + rows)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scrape, "document_service_healthy", lambda: True)
    monkeypatch.setattr(scrape, "build_session", lambda: None)

    def fake_scrape_entity(session, entity_name, entity_val, status, entity_type, doc_type,
                           writer, deadline=None, record_filter=None, on_record_seen=None, **_):
        records = search.get(entity_name, [])
        to_fetch = {r["detail_url"] for r in records if record_filter(r)}
        for r in records:
            on_record_seen(r)
            if record_filter(r) and r["detail_url"] in to_fetch:
                writer.writerow([r["doc_number"], r["doc_type"], r["entity_code"], entity_name,
                                 r["vendor"], r["amount"], r["begin_date"], r["end_date"],
                                 status, r["detail_url"], NEW_VIEW])
        return len(records), True

    monkeypatch.setattr(scrape, "scrape_entity", fake_scrape_entity)
    scrape.run_daily(argparse.Namespace(dataset="contract", entity=list(entities), hours=None))

    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))[1:]


def report(tmp_path):
    path = tmp_path / "data" / "daily_diff_report.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def stamped(tmp_path):
    path = tmp_path / "data" / "scrape_meta.json"
    return path.exists() and "contract" in json.loads(path.read_text(encoding="utf-8"))


def status_of(rows, n):
    return [r[8] for r in rows if r[0] == str(n)]


@pytest.mark.parametrize("active", [1, 4])
def test_a_small_body_ending_everything_flips_and_the_night_finishes(monkeypatch, tmp_path, active):
    rows = [row(n) for n in range(active)] + [row(99, OTHER)]
    out = night(monkeypatch, tmp_path, rows, {OTHER: [listed(99, OTHER)]})

    assert all(status_of(out, n) == ["Expired"] for n in range(active))
    assert stamped(tmp_path)
    [entry] = report(tmp_path)
    assert entry["complete"] is True
    assert entry["entities"][CAMPUS]["flipped_to_expired"] == active


def test_five_all_gone_is_refused_and_holds_the_night_open(monkeypatch, tmp_path):
    rows = [row(n) for n in range(5)] + [row(99, OTHER)]
    out = night(monkeypatch, tmp_path, rows, {OTHER: [listed(99, OTHER)]})

    assert all(status_of(out, n) == ["Active"] for n in range(5))
    assert not stamped(tmp_path)
    [entry] = report(tmp_path)
    assert CAMPUS not in entry["entities"]


def test_an_unfinished_night_still_reports_what_it_patched(monkeypatch, tmp_path, capsys):
    """Six of ten gone is under the scrape's 20-record refusal but over the guard
    rail's half, and OTHER is refused, so the night never finishes. The six
    flips are patched regardless -- so the guard rail has to see them."""
    rows = [row(n) for n in range(10)] + [row(90 + n, OTHER) for n in range(5)]
    out = night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(4)]})

    assert sum(status_of(out, n) == ["Expired"] for n in range(10)) == 6
    assert not stamped(tmp_path), "a night that did not finish must not claim freshness"
    [entry] = report(tmp_path)
    assert entry["complete"] is False
    assert entry["timestamp"]
    assert entry["entities"] == {CAMPUS: {"previously_active": 10, "still_active": 4,
                                          "newly_active": 0, "flipped_to_expired": 6}}

    monkeypatch.setattr(check_daily_diff, "REPORT", str(tmp_path / "data" / "daily_diff_report.json"))
    assert check_daily_diff.main() == 1
    assert "unfinished night" in capsys.readouterr().out


def test_a_same_day_rerun_reports_each_entity_once(monkeypatch, tmp_path):
    rows = [row(n) for n in range(10)] + [row(90 + n, OTHER) for n in range(5)]
    search = {CAMPUS: [listed(n) for n in range(9)]}
    night(monkeypatch, tmp_path, rows, search)
    night(monkeypatch, tmp_path, None, search)       # OTHER refused again

    [entry] = report(tmp_path)
    assert entry["complete"] is False
    assert entry["entities"][CAMPUS]["flipped_to_expired"] == 1

    search[OTHER] = [listed(90 + n, OTHER) for n in range(5)]
    night(monkeypatch, tmp_path, None, search)       # the state answers; the night finishes

    [entry] = report(tmp_path)
    assert entry["complete"] is True
    assert set(entry["entities"]) == {CAMPUS, OTHER}
    assert entry["entities"][CAMPUS]["flipped_to_expired"] == 1
    assert entry["totals"]["previously_active"] == 15
    assert stamped(tmp_path)
