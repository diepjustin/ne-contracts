"""A fresh clone must not be able to stamp an empty dataset complete.

data/*_scrape_progress.json is tracked in git and data/*.csv is not, so a clone
says every combo is finished and holds no rows. The full-scrape path used to
take that at its word: it scraped the nothing that was left, wrote a header, and
recorded "All combos complete" with the time -- a fresh stamp on an empty
dataset, followed by a build error that blames the vendor groups. Nothing here
makes a network call: the session, the health check, the search and the stamp are
all replaced.
"""

import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import scrape  # noqa: E402

DATASET = "contract"
CSV = scrape.DATASETS[DATASET][2]
PROGRESS = scrape.progress_file(DATASET)
ENTITY = sorted(scrape.HIGHER_ED_ENTITIES)[0]


def row(entity=ENTITY, status="Expired"):
    out = [""] * len(scrape.CSV_HEADER)
    out[scrape.CSV_HEADER.index("Entity Name")] = entity
    out[scrape.CSV_HEADER.index("Status")] = status
    out[scrape.CSV_HEADER.index("Detail URL")] = "https://example.invalid/detail/1"
    return out


def write_csv(*rows):
    import csv

    with open(CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(scrape.CSV_HEADER)
        writer.writerows(rows)


def finish_every_combo():
    done = {(name, status) for name, _, status in scrape.combos(scrape.HIGHER_ED_ENTITIES)}
    scrape.save_progress(PROGRESS, done, {})


@pytest.fixture
def run(monkeypatch, tmp_path):
    """scrape.main() in an empty directory, with everything that touches the network replaced."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data")
    calls = {"scraped": [], "stamped": []}

    def scrape_entity(session, name, value, status, *args, **kwargs):
        calls["scraped"].append((name, status))
        return 0, True

    def record_scrape_time(dataset):
        calls["stamped"].append(dataset)
        return "STAMP"

    monkeypatch.setattr(scrape, "document_service_healthy", lambda: True)
    monkeypatch.setattr(scrape, "build_session", lambda: object())
    monkeypatch.setattr(scrape, "scrape_entity", scrape_entity)
    monkeypatch.setattr(scrape, "record_scrape_time", record_scrape_time)

    def go(*argv):
        monkeypatch.setattr(sys, "argv", ["scrape.py", DATASET, *argv])
        scrape.main()

    go.calls = calls
    return go


def refused(run, capsys, *argv):
    with pytest.raises(SystemExit) as stop:
        run(*argv)
    assert stop.value.code == 1
    return capsys.readouterr().err


# --- the refusal ---------------------------------------------------------------

def test_a_fresh_clone_is_refused_not_stamped_complete(run, capsys):
    finish_every_combo()
    err = refused(run, capsys)
    assert "does not exist" in err and "--fresh" in err and "Getting the data back" in err
    assert run.calls == {"scraped": [], "stamped": []}
    assert not os.path.exists(CSV), "a refused run must not leave a header-only CSV behind"


def test_a_header_only_csv_is_refused_too(run, capsys):
    finish_every_combo()
    write_csv()
    assert "has no records" in refused(run, capsys)
    assert run.calls == {"scraped": [], "stamped": []}


def test_a_run_that_was_under_way_is_refused_when_its_rows_are_gone(run, capsys):
    """Progress that only has a page position is still progress the CSV cannot back."""
    scrape.save_progress(PROGRESS, set(), {(ENTITY, "Active"): 12})
    refused(run, capsys)
    assert run.calls["scraped"] == []


# --- what it must leave alone ----------------------------------------------------------

def test_a_restored_dataset_is_left_alone(run):
    finish_every_combo()
    write_csv(row())
    before = open(CSV, "rb").read()
    run()
    assert run.calls["scraped"] == []
    assert open(CSV, "rb").read() == before


@pytest.mark.xfail(strict=True, reason=(
    "Known, not fixed here: with every combo done a full run fetches nothing and still "
    "records the time as now, so a restored dataset reads as freshly scraped. Closest "
    "audit finding is scraper-03 (--entity runs stamp the whole dataset); this case "
    "needs its own rule, e.g. stamp only when the run scraped something. When it is "
    "fixed this test passes, strict=True fails it, and the marker should go."))
def test_a_run_that_scraped_nothing_does_not_stamp_the_dataset_fresh(run):
    finish_every_combo()
    write_csv(row())
    run()
    assert run.calls["stamped"] == []


def test_a_first_run_has_nothing_to_refuse(run):
    run("--entity", ENTITY)
    assert run.calls["scraped"] == [(ENTITY, "Active"), (ENTITY, "Expired")]
    assert os.path.exists(CSV)


def test_fresh_starts_over_instead_of_being_refused(run):
    finish_every_combo()
    run("--fresh", "--entity", ENTITY)
    assert run.calls["scraped"] == [(ENTITY, "Active"), (ENTITY, "Expired")]


def test_status_still_reports_on_a_fresh_clone(run, capsys):
    finish_every_combo()
    run("--status")
    out = capsys.readouterr().out
    assert "REMAINING=0" in out
    assert run.calls == {"scraped": [], "stamped": []}


def test_the_nightly_path_is_not_touched(monkeypatch, tmp_path):
    """--daily has its own entry and its own guard; this one must not reach it."""
    seen = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scrape, "run_daily", lambda args: seen.append(args.dataset))
    monkeypatch.setattr(sys, "argv", ["scrape.py", DATASET, "--daily"])
    scrape.main()
    assert seen == [DATASET]


# --- the helper ------------------------------------------------------------------------

@pytest.mark.parametrize("content, expected", [
    (None, False),                                  # no file
    (b"", False),                                   # empty
    (b"a,b\r\n", False),                            # header only
    (b"a,b", False),                                # header with no newline
    (b"a,b\r\n\r\n  \r\n", False),                  # header and blank lines
    (b"a,b\r\n1,2\r\n", True),
    (b"a,b\r\n1,2", True),                          # a row with no newline is still a row
    (b"a,b\r\n\xff\xfe,2\r\n", True),               # undecodable bytes are not a reason to say "empty"
])
def test_csv_has_records(tmp_path, content, expected):
    path = tmp_path / "x.csv"
    if content is not None:
        path.write_bytes(content)
    assert scrape.csv_has_records(str(path)) is expected
