"""pages.yml's merge of the released document log into the cached nightly one.

The newest entry per record has to come out last, because load_documents folds
last-entry-wins. Until Nov 2026 the merge was `cat cached release`, so the 28-30 Aug
backfill won every record both held, and the site showed the August document list on
207 records the nightly had re-read since: 235 documents hidden, usually the amendment.
Every test here runs the real merge and reads the result back the way the build does.
"""

import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import merge_document_logs as merging  # noqa: E402
import scrape  # noqa: E402


def entry(k, n, seen, **extra):
    return {"k": k, "doc": "D-" + k, "entity": "E", "n": n, "seen": seen, **extra}


def write_log(path, entries, tail=b""):
    with open(path, "wb") as f:
        for e in entries:
            f.write(json.dumps(e).encode() + b"\n")
        f.write(tail)


@pytest.fixture
def logs(tmp_path):
    """(release path, cached path, a function that merges and folds the result)."""
    release, cached, out = (str(tmp_path / n) for n in ("release.jsonl", "cached.jsonl", "out.jsonl"))

    def merged():
        counts = merging.merge(release, cached, out)
        return scrape.load_documents(out), counts

    return release, cached, merged


def test_a_record_the_nightly_re_read_keeps_the_nightly_list(logs):
    """The 5 Nov shape: the cached log holds the backfill's entry, then a newer one."""
    release, cached, merged = logs
    write_log(release, [entry("amended", 1, "2026-08-29"), entry("backfilled", 3, "2026-08-29")])
    write_log(cached, [entry("amended", 1, "2026-08-29"),
                       entry("nightly-only", 2, "2026-09-30"),
                       entry("amended", 2, "2026-10-14")])
    store, counts = merged()
    assert sorted(store) == ["amended", "backfilled", "nightly-only"]
    assert store["amended"]["n"] == 2, "the amendment the nightly re-read, not the August list"
    assert store["nightly-only"]["n"] == 2
    assert store["backfilled"]["n"] == 3, "a record only the release has still comes through"
    assert counts == {"cached": 3, "release": 2, "release_newer": 0, "release_only": 1}


def test_a_backfill_re_uploaded_after_the_nightly_read_wins(logs):
    """Why the merge goes by date and not by putting the release first: a later
    backfill sitting, re-uploaded, is newer than the nightly's reads."""
    release, cached, merged = logs
    write_log(cached, [entry("rechecked", 1, "2026-10-01")])
    write_log(release, [entry("rechecked", 4, "2026-11-20")])
    store, counts = merged()
    assert store["rechecked"]["n"] == 4
    assert counts["release_newer"] == 1


def test_the_cached_entry_wins_a_tie_and_a_dateless_release_entry_never_wins(logs):
    release, cached, merged = logs
    write_log(cached, [entry("tie", 1, "2026-08-29"), entry("dated", 1, "2026-08-29")])
    write_log(release, [entry("tie", 9, "2026-08-29"),
                        {"k": "dated", "doc": "D", "entity": "E", "n": 9}])
    store, counts = merged()
    assert store["tie"]["n"] == 1 and store["dated"]["n"] == 1
    assert counts["release_newer"] == 0


def test_a_cached_record_s_newest_entry_counts_not_its_last(logs):
    """A release entry has to beat every cached entry for its record, not only the last."""
    release, cached, merged = logs
    write_log(cached, [entry("x", 1, "2026-10-01"), entry("x", 2, "2026-09-01")])
    write_log(release, [entry("x", 9, "2026-09-15")])
    store, _counts = merged()
    assert store["x"]["n"] == 2, "the cached log's own last entry stands; the release is not newer"


def test_the_cached_log_is_written_through_unchanged(logs, tmp_path):
    """On 5 Nov 2026 the release added nothing, and the merge was byte-identical to
    the cached log -- the cheapest proof that nothing the nightly read is lost."""
    release, cached, _merged = logs
    write_log(cached, [entry("a", 1, "2026-08-29"), entry("b", 2, "2026-10-01")])
    write_log(release, [entry("a", 1, "2026-08-29"), entry("b", 1, "2026-08-29")])
    out = str(tmp_path / "out.jsonl")
    merging.merge(release, cached, out)
    with open(out, "rb") as a, open(cached, "rb") as b:
        assert a.read() == b.read()


@pytest.mark.parametrize("torn", ["release", "cached"])
def test_a_torn_last_line_in_either_log_is_dropped_not_glued_to_the_next(logs, torn, capsys):
    """A write killed mid-line leaves a partial last line. Glued to the next file it
    would be one broken line in the middle of the result, where load_documents halts --
    every build would fail until someone replaced the file."""
    release, cached, merged = logs
    fragment = b'{"k": "torn", "doc": "D'
    write_log(release, [entry("a", 1, "2026-08-29")], tail=fragment if torn == "release" else b"")
    write_log(cached, [entry("b", 1, "2026-10-01")], tail=fragment if torn == "cached" else b"")
    store, _counts = merged()
    assert sorted(store) == ["a", "b"]
    assert "ends mid-write; dropping its last line" in capsys.readouterr().out


def test_a_complete_last_line_without_a_newline_is_kept(logs):
    release, cached, merged = logs
    with open(release, "wb") as f:
        f.write(json.dumps(entry("a", 1, "2026-08-29")).encode() + b"\n" +
                json.dumps(entry("z", 9, "2026-08-29")).encode())
    write_log(cached, [entry("b", 1, "2026-10-01")])
    store, _counts = merged()
    assert sorted(store) == ["a", "b", "z"]


@pytest.mark.parametrize("broken", ["release", "cached"])
def test_a_broken_line_before_the_end_stops_the_merge(logs, broken, tmp_path):
    """Not a torn write: something rewrote the log, and the lines after it cannot be trusted."""
    release, cached, _merged = logs
    good = [entry("a", 1, "2026-08-29"), entry("b", 1, "2026-08-29")]
    for path in (release, cached):
        write_log(path, good)
    with open(release if broken == "release" else cached, "rb") as f:
        text = f.read()
    with open(release if broken == "release" else cached, "wb") as f:
        f.write(b"{not json\n" + text)
    out = str(tmp_path / "out.jsonl")
    with pytest.raises(SystemExit, match="line 1 is not valid JSON"):
        merging.merge(release, cached, out)
    assert not os.path.exists(out), "no half-merged log is left where the build reads"


def test_the_command_line_prints_what_it_added(tmp_path, capsys):
    release, cached, out = (str(tmp_path / n) for n in ("r.jsonl", "c.jsonl", "o.jsonl"))
    write_log(release, [entry("a", 1, "2026-08-29"), entry("b", 1, "2026-08-29")])
    write_log(cached, [entry("a", 2, "2026-10-01")])
    assert merging.main([release, cached, out]) == 0
    assert ("document logs: 1 cached entries kept; of 2 released, 1 added "
            "(1 for records the cached log lacks, 0 newer than its entry), 1 older or the same"
            ) in capsys.readouterr().out
    with pytest.raises(SystemExit):
        merging.main([release, cached])
