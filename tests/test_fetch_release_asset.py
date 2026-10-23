"""fetch_release_asset.py: a missing asset warns, a broken download fails (fresh-10).

`gh` is replaced by a fake that serves a release listing and downloads from a dict, so
nothing here touches the network.
"""

import datetime
import json
import os
import subprocess
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import fetch_release_asset as fra  # noqa: E402

NOW = datetime.datetime(2026, 10, 23, 8, 30, tzinfo=datetime.timezone.utc)
FRESH = "2026-10-22T09:00:00Z"
OLD = "2026-09-20T09:00:00Z"


class FakeGh:
    """releases: [{tag, assets: [{name, size, updated_at}]}], a list per listing call (the last repeats).
    fail_downloads: how many downloads fail before one succeeds. list_fails: the listing errors."""

    def __init__(self, listings, fail_downloads=0, list_fails=False, wrong_size=False):
        self.listings = listings
        self.fail_downloads = fail_downloads
        self.list_fails = list_fails
        self.wrong_size = wrong_size
        self.calls = []
        self.lists = 0

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "api":
            self.lists += 1
            if self.list_fails:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 502: Bad Gateway")
            listing = self.listings[min(self.lists, len(self.listings)) - 1]
            return subprocess.CompletedProcess(args, 0, "".join(json.dumps(r) + "\n" for r in listing), "")
        if args[:2] == ("release", "download"):
            tag, name, directory = args[2], args[args.index("--pattern") + 1], args[args.index("--dir") + 1]
            if self.fail_downloads:
                self.fail_downloads -= 1
                return subprocess.CompletedProcess(args, 1, "", "connection reset by peer")
            listing = self.listings[min(self.lists, len(self.listings)) - 1]
            (release,) = [r for r in listing if r["tag"] == tag]
            (asset,) = [a for a in release["assets"] if a["name"] == name]
            with open(os.path.join(directory, name), "wb") as f:
                f.write(b"x" * (asset["size"] + (1 if self.wrong_size else 0)))
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    def downloads(self):
        return [c[2] for c in self.calls if c[:2] == ("release", "download")]


def release(tag, *assets):
    return {"tag": tag, "assets": [{"name": n, "size": s, "updated_at": u} for n, s, u in assets]}


AI = "ai_summaries.jsonl.gz"
BOTH = [release("ai-summaries-data", (AI, 10, FRESH)),
        release("extraction-data-2026-08-01", (AI, 7, OLD), ("documents.jsonl.gz", 5, OLD)),
        release("extraction-data-2026-07-01", (AI, 6, OLD)),
        release("ne-contracts-hub-data-2026-10-23", ("scrape_meta.json.gz", 3, FRESH))]


def run(tmp_path, monkeypatch, fake, *extra, tag="ai-summaries-data", asset=AI):
    monkeypatch.setattr(fra, "gh", fake)
    sleeps = []
    argv = ["--repo", "o/r", "--dir", str(tmp_path), "--asset", asset,
            "--fallback-prefix", "extraction-data-", "--max-age-days", "14", *extra]
    if tag:
        argv += ["--tag", tag]
    rc = fra.main(argv, sleep=sleeps.append, now=NOW)
    return rc, sleeps


def test_the_fixed_tag_wins_when_it_has_the_asset(tmp_path, monkeypatch, capsys):
    fake = FakeGh([BOTH])
    rc, sleeps = run(tmp_path, monkeypatch, fake)
    assert rc == 0 and sleeps == [] and fake.downloads() == ["ai-summaries-data"]
    assert (tmp_path / AI).stat().st_size == 10
    assert "::warning" not in capsys.readouterr().out


def test_no_fixed_release_yet_falls_back_to_the_newest_extraction_release(tmp_path, monkeypatch):
    fake = FakeGh([BOTH[1:]])
    rc, sleeps = run(tmp_path, monkeypatch, fake)
    assert rc == 0 and sleeps == [], "no release means no rename gap to wait out"
    assert fake.downloads() == ["extraction-data-2026-08-01"]


def test_a_fixed_release_without_the_asset_is_looked_at_twice_then_falls_back(tmp_path, monkeypatch):
    listing = [release("ai-summaries-data")] + BOTH[1:]
    fake = FakeGh([listing])
    rc, sleeps = run(tmp_path, monkeypatch, fake)
    assert rc == 0 and sleeps == [10] and fake.lists == 2
    assert fake.downloads() == ["extraction-data-2026-08-01"]


def test_an_asset_caught_mid_rename_is_found_on_the_second_look(tmp_path, monkeypatch):
    mid_rename = [release("ai-summaries-data", ("next-" + AI, 11, FRESH), ("prev-" + AI, 10, OLD))] + BOTH[1:]
    fake = FakeGh([mid_rename, BOTH])
    rc, sleeps = run(tmp_path, monkeypatch, fake)
    assert rc == 0 and sleeps == [10] and fake.downloads() == ["ai-summaries-data"]


def test_an_asset_old_enough_warns(tmp_path, monkeypatch, capsys):
    rc, _ = run(tmp_path, monkeypatch, FakeGh([BOTH[1:]]))
    out = capsys.readouterr().out
    assert rc == 0 and "::warning title=Stale release asset::" in out and "33 days ago" in out


def test_age_is_not_checked_unless_asked(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fra, "gh", FakeGh([BOTH]))
    assert fra.main(["--repo", "o/r", "--dir", str(tmp_path), "--asset", "documents.jsonl.gz",
                     "--fallback-prefix", "extraction-data-"], sleep=lambda s: None, now=NOW) == 0
    assert "::warning" not in capsys.readouterr().out


def test_nowhere_to_be_found_warns_and_writes_nothing(tmp_path, monkeypatch, capsys):
    rc, _ = run(tmp_path, monkeypatch, FakeGh([[BOTH[3]]]))
    out = capsys.readouterr().out
    assert rc == 0 and "::warning title=Release asset not found::" in out
    assert not (tmp_path / AI).exists()


def test_a_stale_copy_left_in_the_directory_is_not_mistaken_for_a_download(tmp_path, monkeypatch):
    (tmp_path / AI).write_bytes(b"from an earlier run")
    fake = FakeGh([BOTH], fail_downloads=2)
    rc, _ = run(tmp_path, monkeypatch, fake)
    assert rc == 1 and not (tmp_path / AI).exists()


def test_one_failed_download_is_retried(tmp_path, monkeypatch):
    fake = FakeGh([BOTH], fail_downloads=1)
    rc, sleeps = run(tmp_path, monkeypatch, fake)
    assert rc == 0 and sleeps == [10] and fake.downloads() == ["ai-summaries-data"] * 2


def test_two_failed_downloads_fail_the_step_rather_than_falling_back(tmp_path, monkeypatch, capsys):
    fake = FakeGh([BOTH], fail_downloads=2)
    rc, _ = run(tmp_path, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1 and "::error title=Release download failed::" in out and "connection reset" in out
    assert fake.downloads() == ["ai-summaries-data"] * 2, "a broken download must not look like a missing one"


def test_a_truncated_download_fails(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, FakeGh([BOTH], wrong_size=True))[0] == 1


def test_a_listing_failure_fails_the_step(tmp_path, monkeypatch, capsys):
    rc, _ = run(tmp_path, monkeypatch, FakeGh([BOTH], list_fails=True))
    assert rc == 1 and "HTTP 502" in capsys.readouterr().out


@pytest.mark.parametrize("tags, expected", [
    (["extraction-data-2026-07-01", "extraction-data-2026-08-01"], ["x", "extraction-data-2026-08-01"]),
    ([], ["x"]),
    (["x"], ["x"]),
])
def test_candidates_are_the_tag_then_the_newest_prefixed_release(tags, expected):
    releases = [{"tag": t, "assets": []} for t in tags]
    assert fra.candidates(releases, "x", "extraction-data-" if tags != ["x"] else "x") == expected
