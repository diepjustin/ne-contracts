"""--daily across several nights: subsets, restores, held-back rows and strays that persist.

test_status_flips.py pins one night's decisions. These pin what those decisions add up to
over a run of nights, the cases the Oct 2026 audit found unpinned (diff-01, diff-02, diff-06,
sc-04, sc-06): a `--entity` run stamping a dataset it did not read, a restored record keeping
the amount and end date it had when it left the list, a leaky night appending records the
state lists as Expired, and strays that never go away freezing an entity's flip-backs.

Nothing here touches the network: the search is scripted by test_status_flips.night().
"""

import json
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.join(os.path.dirname(TESTS), "scripts"))

import scrape  # noqa: E402
from test_status_flips import (CAMPUS, OTHER, listed, night, report, row,  # noqa: E402
                               stamped, status_of)


# --- sc-06: a subset run never stamps the dataset -------------------------------------------

def test_an_entity_subset_does_not_stamp_the_whole_dataset(monkeypatch, tmp_path, capsys):
    rows = [row(n) for n in range(3)] + [row(99, OTHER)]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(3)]}, only=(CAMPUS,))

    assert not stamped(tmp_path), "OTHER was never read, so the dataset is not fresh"
    [entry] = report(tmp_path)
    assert entry["complete"] is False and set(entry["entities"]) == {CAMPUS}
    printed = capsys.readouterr().out
    assert "limited by --entity" in printed and OTHER in printed


def test_a_subset_that_finishes_the_rest_of_the_day_does_stamp(monkeypatch, tmp_path):
    """The subset is not the problem; completeness against the full list is the test."""
    rows = [row(n) for n in range(3)] + [row(99, OTHER)]
    search = {CAMPUS: [listed(n) for n in range(3)], OTHER: [listed(99, OTHER)]}
    night(monkeypatch, tmp_path, rows, search, only=(CAMPUS,))
    night(monkeypatch, tmp_path, None, search, only=(OTHER,))

    assert stamped(tmp_path)
    [entry] = report(tmp_path)
    assert entry["complete"] is True and set(entry["entities"]) == {CAMPUS, OTHER}


# --- diff-03: the flip ledger is hashed and pruned ------------------------------------------

def _url(n):
    return f"https://example.test/detail?D={n}"


def test_a_ledger_of_raw_urls_loads_under_hashed_keys(tmp_path):
    path = tmp_path / "flips.json"
    path.write_text(json.dumps({_url(1): "2026-10-20", scrape.ledger_key(_url(2)): "2026-10-21"}),
                    encoding="utf-8")
    assert scrape.load_flip_ledger(str(path)) == {scrape.ledger_key(_url(1)): "2026-10-20",
                                                  scrape.ledger_key(_url(2)): "2026-10-21"}


def test_ledger_keys_are_the_expired_only_keys():
    assert scrape.ledger_key(_url(1)) == scrape.document_key(_url(1))
    assert scrape.ledger_key(scrape.ledger_key(_url(1))) == scrape.ledger_key(_url(1))


def test_pruning_drops_old_entries_and_ones_no_longer_expired_only(tmp_path):
    import datetime

    today = datetime.date(2026, 10, 23)
    path = tmp_path / "flips.json"
    path.write_text(json.dumps({
        _url(1): "2026-10-22",                     # owed, recent: kept
        _url(2): "2025-10-22",                     # 366 days old: dropped
        _url(3): "2025-10-23",                     # exactly the limit: kept
        _url(4): "2026-10-22",                     # its row is Active again: dropped
        _url(5): "not a date",                     # unreadable date: kept, doubt keeps it
    }), encoding="utf-8")
    expired_only = {scrape.document_key(_url(n)) for n in (1, 2, 3, 5)}
    pruned = scrape.load_flip_ledger(str(path), expired_only, today=today)
    assert set(pruned) == {scrape.ledger_key(_url(n)) for n in (1, 3, 5)}


def test_a_night_saves_the_pruned_ledger(monkeypatch, tmp_path):
    """Record 19 was flipped and then re-read Active by hand: the next night's ledger drops it."""
    from test_status_flips import keys, ledger

    rows = [row(n) for n in range(20)]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(19)]}, entities=(CAMPUS,))
    assert set(ledger(tmp_path)) == keys(19)

    csv_path = tmp_path / "data" / "nu_contracts.csv"
    text = csv_path.read_text(encoding="utf-8").replace(",Expired,", ",Active,")
    csv_path.write_text(text, encoding="utf-8")            # row 19 Active again, outside --daily
    night(monkeypatch, tmp_path, None, {CAMPUS: [listed(n) for n in range(18)] + [listed(19)]},
          entities=(CAMPUS,))
    assert set(ledger(tmp_path)) == keys(18), "19's stale entry pruned, tonight's flip of 18 added"


# --- diff-02: a record flipped back carries tonight's listing --------------------------------

def _changes(tmp_path):
    path = tmp_path / "data" / "changes.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_restored_record_takes_tonights_amount_end_date_and_view_url(monkeypatch, tmp_path):
    """The 20-22 Oct 2026 shape: 12 of 13 restored records were published Active with an end
    date already passed, because only their Status moved. Contract 9 is extended while it is
    off the list; when it returns, its row has to say so."""
    from test_status_flips import RESTORED_VIEW, ledger

    rows = [row(n) for n in range(10)]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(9)]}, entities=(CAMPUS,))
    extended = dict(listed(9, amount="$250.00"), end_date="06/30/2028", vendor="ACME SUPPLY LLC")
    out = night(monkeypatch, tmp_path, None, {CAMPUS: [listed(n) for n in range(9)] + [extended]},
                entities=(CAMPUS,))

    [r9] = [r for r in out if r[0] == "9"]
    assert r9 == ["9", "Contract", "050", CAMPUS, "ACME SUPPLY LLC", "$250.00", "07/01/2026",
                  "06/30/2028", "Active", "https://example.test/detail?D=9", RESTORED_VIEW]
    assert len(out) == 10, "restored in place, not appended"
    assert ledger(tmp_path) == {}
    assert {(c["doc"], c["field"], c["from"], c["to"]) for c in _changes(tmp_path)} == {
        ("9", "amount", "$100.00", "$250.00"), ("9", "end date", "06/30/2027", "06/30/2028"),
        ("9", "vendor", "ACME SUPPLY", "ACME SUPPLY LLC")}
    assert all(c["seen"] == scrape.datetime.date.today().isoformat() for c in _changes(tmp_path))
    [entry] = night.documents
    assert entry["k"] == scrape.document_key("https://example.test/detail?D=9") and entry["n"] == 1


def test_a_restore_whose_detail_page_fails_waits_a_night(monkeypatch, tmp_path, capsys):
    """Like a new record whose detail page cannot be read: nothing unknown is written as fact."""
    from test_status_flips import keys, ledger

    url9 = "https://example.test/detail?D=9"
    rows = [row(n) for n in range(10)]
    everything = {CAMPUS: [listed(n) for n in range(10)]}
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(9)]}, entities=(CAMPUS,))
    capsys.readouterr()

    out = night(monkeypatch, tmp_path, None, everything, entities=(CAMPUS,), documents={url9: None})
    assert status_of(out, 9) == ["Expired"] and set(ledger(tmp_path)) == keys(9), "still owed"
    assert report(tmp_path)[-1]["entities"][CAMPUS]["flipped_back"] == 0
    assert night.documents == []
    assert "could not have their detail pages read" in capsys.readouterr().out

    out = night(monkeypatch, tmp_path, None, everything, entities=(CAMPUS,))
    assert status_of(out, 9) == ["Active"] and ledger(tmp_path) == {}


def test_twins_end_with_one_active_row_carrying_tonights_values(monkeypatch, tmp_path):
    """A URL the full scrape read Active and then Expired, flipped by --daily and listed again:
    whichever row is restored, the URL is listed Active once, with tonight's values."""
    from test_status_flips import RESTORED_VIEW

    rows = [row(n) for n in range(9)] + [row(9), row(9, status="Expired", amount="$999.00")]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(9)]}, entities=(CAMPUS,))
    out = night(monkeypatch, tmp_path, None,
                {CAMPUS: [listed(n) for n in range(9)] + [listed(9, amount="$300.00")]}, entities=(CAMPUS,))

    active9 = [r for r in out if r[0] == "9" and r[8] == "Active"]
    assert len(active9) == 1
    assert (active9[0][5], active9[0][10]) == ("$300.00", RESTORED_VIEW)


def test_a_genuinely_reopened_stray_is_restored_with_tonights_values(monkeypatch, tmp_path):
    from test_status_flips import RESTORED_VIEW, full_scrape_expired

    rows = [row(n) for n in range(100)] + [full_scrape_expired(500)]
    out = night(monkeypatch, tmp_path, rows,
                {CAMPUS: [listed(n) for n in range(100)] + [dict(listed(500), end_date="12/31/2027")]},
                entities=(CAMPUS,))
    [r500] = [r for r in out if r[0] == "500"]
    assert (r500[7], r500[8], r500[10]) == ("12/31/2027", "Active", RESTORED_VIEW)


# --- sc-04: a leaky night appends nothing it has never seen ------------------------------------

def _leaky_night(monkeypatch, tmp_path):
    """CAMPUS lists its 20 Active records, the 40 Expired ones the CSV holds, and 10 Expired
    ones the CSV never held -- POs the state published already expired, say."""
    from test_status_flips import full_scrape_expired

    rows = [row(n) for n in range(20)] + [full_scrape_expired(500 + n) for n in range(40)] + [row(99, OTHER)]
    leaking = ([listed(n) for n in range(20)] + [listed(500 + n) for n in range(40)]
               + [listed(800 + n) for n in range(10)])
    return night(monkeypatch, tmp_path, rows, {CAMPUS: leaking, OTHER: [listed(99, OTHER)]})


def test_a_leaky_night_holds_back_records_it_has_never_seen(monkeypatch, tmp_path, capsys):
    out = _leaky_night(monkeypatch, tmp_path)

    assert all(status_of(out, 800 + n) == [] for n in range(10)), "not appended as Active"
    assert len(out) == 61
    counts = report(tmp_path)[-1]["entities"][CAMPUS]
    assert (counts["newly_active"], counts["held_back"]) == (0, 10)
    assert report(tmp_path)[-1]["totals"]["held_back"] == 10
    printed = capsys.readouterr().out
    assert "appending none" not in printed
    assert "holding back the records the CSV has never seen" in printed
    assert "Held back 10 record(s) the CSV has never seen" in printed
    assert stamped(tmp_path), "the night's endings stand and it finishes, as before"


def test_held_back_records_are_appended_once_the_filter_holds(monkeypatch, tmp_path):
    """If the state really does list them Active, the first clean night appends them."""
    _leaky_night(monkeypatch, tmp_path)
    search = {CAMPUS: [listed(n) for n in range(20)] + [listed(800 + n) for n in range(10)],
              OTHER: [listed(99, OTHER)]}
    out = night(monkeypatch, tmp_path, None, search)
    assert all(status_of(out, 800 + n) == ["Active"] for n in range(10))
    assert report(tmp_path)[-1]["entities"][CAMPUS]["newly_active"] == 10


def test_another_entitys_new_rows_are_written_on_its_leak_free_night(monkeypatch, tmp_path):
    from test_status_flips import full_scrape_expired

    rows = [row(n) for n in range(20)] + [full_scrape_expired(500 + n) for n in range(40)] + [row(99, OTHER)]
    leaking = [listed(n) for n in range(20)] + [listed(500 + n) for n in range(40)] + [listed(800)]
    out = night(monkeypatch, tmp_path, rows,
                {CAMPUS: leaking, OTHER: [listed(99, OTHER)] + [listed(1000 + n, OTHER) for n in range(1000)]})
    assert status_of(out, 800) == []
    assert all(status_of(out, 1000 + n) == ["Active"] for n in range(1000)), \
        "40 strays are not a leak across 1,061 listed, so OTHER's new rows stand"


def test_a_thin_leak_across_the_dataset_holds_back_every_entitys_new_rows(monkeypatch, tmp_path):
    from test_status_flips import full_scrape_expired

    rows = ([row(n) for n in range(3)] + [full_scrape_expired(500 + n) for n in range(10)]
            + [row(900 + n, OTHER) for n in range(3)] + [full_scrape_expired(600 + n, OTHER) for n in range(10)])
    search = {CAMPUS: [listed(n) for n in range(3)] + [listed(500 + n) for n in range(4)] + [listed(800)],
              OTHER: [listed(900 + n, OTHER) for n in range(3)] + [listed(600 + n, OTHER) for n in range(4)]
              + [listed(801, OTHER)]}
    out = night(monkeypatch, tmp_path, rows, search)
    assert status_of(out, 800) == [] and status_of(out, 801) == []
    assert report(tmp_path)[-1]["totals"]["held_back"] == 2


def test_a_refused_entity_flips_nothing_back_but_keeps_its_new_rows(monkeypatch, tmp_path):
    """Refused: 40 of 60 vanished. A record it flipped earlier is listed again, and a new one
    appears. Nothing of a refused entity is patched -- the flip-back included, which stays
    owed -- but what it lists that the CSV has never seen is written, as it always was."""
    from test_status_flips import keys, ledger

    # OTHER is large enough that 40 vanishing is not the dataset-wide breaker.
    rows = [row(n) for n in range(61)] + [row(1000 + n, OTHER) for n in range(100)]
    other = [listed(1000 + n, OTHER) for n in range(100)]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(60)], OTHER: other})
    assert set(ledger(tmp_path)) == keys(60)

    out = night(monkeypatch, tmp_path, None,
                {CAMPUS: [listed(n) for n in range(20)] + [listed(60), listed(700)], OTHER: other})
    assert status_of(out, 60) == ["Expired"] and set(ledger(tmp_path)) == keys(60)
    assert all(status_of(out, n) == ["Active"] for n in range(60)), "refused: nothing expired"
    assert status_of(out, 700) == ["Active"]
    entry = report(tmp_path)[-1]
    assert entry["complete"] is False and CAMPUS not in entry["entities"]


# --- diff-01: strays that never go away turn the run red -------------------------------------
#
# A record --daily flipped is owed a flip back only while the ledger names it. Lose the
# ledger (a cache rebuilt from release CSVs, a file set aside as corrupt), or have records
# flipped before the ledger existed, and their return reads as strays -- every night, since
# they do not go away. Six of a hundred is a leak, so the entity's flip-backs were held back
# night after night, with a warning and a green run.

import datetime  # noqa: E402

DAY = datetime.date(2026, 10, 20)


def on(n):
    return DAY + datetime.timedelta(days=n)


def streaks(tmp_path):
    path = tmp_path / "data" / "contract_daily_leaks.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def unstamp(tmp_path):
    meta = tmp_path / "data" / "scrape_meta.json"
    if meta.exists():
        meta.unlink()


ERROR = "::error title=Daily scrape: flip-backs held back 3 nights running::contract: "


def _lost_ledger(monkeypatch, tmp_path):
    rows = [row(n) for n in range(100)]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(94)]},
          entities=(CAMPUS,), today=on(0))
    (tmp_path / "data" / "contract_daily_flips.json").unlink()
    return {CAMPUS: [listed(n) for n in range(100)]}


def test_a_lost_ledger_turns_the_run_red_on_the_third_leak_night(monkeypatch, tmp_path, capsys):
    search = _lost_ledger(monkeypatch, tmp_path)
    capsys.readouterr()
    for n in (1, 2):
        unstamp(tmp_path)
        out = night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(n))
        printed = capsys.readouterr().out
        assert "::warning title=Daily scrape: the Active search is listing Expired records::" in printed
        assert "::error" not in printed
        assert stamped(tmp_path), f"night {n} is still green"
        assert all(status_of(out, k) == ["Expired"] for k in range(94, 100))
        assert streaks(tmp_path)[CAMPUS]["nights"] == n

    unstamp(tmp_path)
    night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(3))
    printed = capsys.readouterr().out
    assert ERROR + CAMPUS + ":" in printed
    assert "94, 95, 96, 97, 98" in printed, "names strays a person can look up"
    assert "--accept-strays" in printed
    assert not stamped(tmp_path), "unstamped, so check_scrape_stamps.py turns the run red"
    assert report(tmp_path)[-1]["complete"] is False


def test_the_red_night_survives_a_same_day_rerun(monkeypatch, tmp_path, capsys):
    search = _lost_ledger(monkeypatch, tmp_path)
    for n in (1, 2, 3):
        night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(n))
    unstamp(tmp_path)
    capsys.readouterr()
    night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(3))
    assert not stamped(tmp_path)
    assert ERROR in capsys.readouterr().out
    assert streaks(tmp_path)[CAMPUS]["nights"] == 3, "a re-run the same day adds no night"


def test_accept_strays_restores_them_and_ends_the_streak(monkeypatch, tmp_path, capsys):
    from test_status_flips import RESTORED_VIEW

    search = _lost_ledger(monkeypatch, tmp_path)
    for n in (1, 2, 3):
        night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(n))
    capsys.readouterr()

    out = night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(3),
                only=(CAMPUS,), accept_strays=True)
    assert all(status_of(out, k) == ["Active"] for k in range(94, 100))
    assert all(r[10] == RESTORED_VIEW for r in out if int(r[0]) >= 94), "through the restore path"
    assert len(out) == 100
    assert streaks(tmp_path) == {}
    assert stamped(tmp_path)
    assert "--accept-strays: taking 6 stray(s) as genuinely re-opened" in capsys.readouterr().out


def test_a_clean_night_ends_a_streak(monkeypatch, tmp_path):
    search = _lost_ledger(monkeypatch, tmp_path)
    night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(1))
    night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(2))
    night(monkeypatch, tmp_path, None, {CAMPUS: [listed(n) for n in range(94)]},
          entities=(CAMPUS,), today=on(3))
    assert streaks(tmp_path) == {}
    night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(4))
    assert streaks(tmp_path)[CAMPUS]["nights"] == 1


def test_a_refused_night_leaves_a_streak_where_it_was(monkeypatch, tmp_path):
    """A refused entity's listing says nothing about its filter, either way."""
    search = _lost_ledger(monkeypatch, tmp_path)
    night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(1))
    night(monkeypatch, tmp_path, None, {CAMPUS: [listed(n) for n in range(10)]},
          entities=(CAMPUS,), today=on(2))
    assert streaks(tmp_path)[CAMPUS]["nights"] == 1
    night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(3))
    assert streaks(tmp_path)[CAMPUS]["nights"] == 2


def test_genuine_reopenings_at_a_small_entity_go_red_and_can_be_accepted(monkeypatch, tmp_path, capsys):
    """20 Active; --daily flips one; then the state re-opens 5 contracts the full scrape filed
    Expired. Five strays of 25 is a leak, and it held the entity's own ledgered flip hostage."""
    from test_status_flips import full_scrape_expired

    rows = [row(n) for n in range(20)] + [full_scrape_expired(500 + n) for n in range(5)]
    night(monkeypatch, tmp_path, rows, {CAMPUS: [listed(n) for n in range(19)]},
          entities=(CAMPUS,), today=on(0))
    search = {CAMPUS: [listed(n) for n in range(20)] + [listed(500 + n) for n in range(5)]}
    for n in (1, 2, 3):
        unstamp(tmp_path)
        out = night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(n))
        assert status_of(out, 19) == ["Expired"]
    assert not stamped(tmp_path) and ERROR + CAMPUS in capsys.readouterr().out

    out = night(monkeypatch, tmp_path, None, search, entities=(CAMPUS,), today=on(4),
                only=(CAMPUS,), accept_strays=True)
    assert status_of(out, 19) == ["Active"]
    assert all(status_of(out, 500 + n) == ["Active"] for n in range(5))
    assert stamped(tmp_path)


def test_a_thin_leak_across_the_dataset_keeps_its_own_streak(monkeypatch, tmp_path, capsys):
    from test_status_flips import full_scrape_expired

    rows = ([row(n) for n in range(3)] + [full_scrape_expired(500 + n) for n in range(10)]
            + [row(900 + n, OTHER) for n in range(3)] + [full_scrape_expired(600 + n, OTHER) for n in range(10)])
    search = {CAMPUS: [listed(n) for n in range(3)] + [listed(500 + n) for n in range(4)],
              OTHER: [listed(900 + n, OTHER) for n in range(3)] + [listed(600 + n, OTHER) for n in range(4)]}
    night(monkeypatch, tmp_path, rows, search, today=on(1))
    night(monkeypatch, tmp_path, None, search, today=on(2))
    assert set(streaks(tmp_path)) == {scrape.DATASET_STREAK}, "neither entity leaks on its own"
    unstamp(tmp_path)
    capsys.readouterr()
    night(monkeypatch, tmp_path, None, search, today=on(3))
    printed = capsys.readouterr().out
    assert ERROR + scrape.DATASET_STREAK in printed
    assert f'--daily --entity "{CAMPUS}" --entity "{OTHER}" --accept-strays' in printed
    assert f"{CAMPUS}: 500" in printed, "strays named with their entity"
    assert not stamped(tmp_path)

    # Accepting both entities clears the dataset's count too: each was under the line alone.
    out = night(monkeypatch, tmp_path, None, search, today=on(3), only=(CAMPUS, OTHER), accept_strays=True)
    from test_status_flips import RESTORED_VIEW
    returned = [r for r in out if 500 <= int(r[0]) < 700 and r[8] == "Active"]
    assert len(returned) == 8 and all(r[10] == RESTORED_VIEW for r in returned)
    assert streaks(tmp_path) == {}
    assert stamped(tmp_path)


def test_an_unreadable_streak_file_is_set_aside_and_started_fresh(tmp_path, capsys):
    path = tmp_path / "leaks.json"
    path.write_text('{"Chadron', encoding="utf-8")
    assert scrape.load_leak_streaks(str(path)) == {}
    assert "::warning title=Unreadable state file::" in capsys.readouterr().out


def test_accept_strays_is_refused_without_daily_and_an_entity(monkeypatch, capsys):
    for argv in (["contract", "--daily", "--accept-strays"],
                 ["contract", "--entity", CAMPUS, "--accept-strays"]):
        monkeypatch.setattr(sys, "argv", ["scrape.py"] + argv)
        try:
            scrape.main()
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError(f"{argv} was accepted")
    assert "--accept-strays needs --daily and at least one --entity" in capsys.readouterr().err
