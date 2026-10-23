"""check_live_site.py: the nightly goes red when the live site is not the build it dispatched (sc-01).

No network: the fetcher and the sleep are injected.
"""

import os
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import build_site  # noqa: E402
import check_live_site  # noqa: E402

DISPATCHED = "2026-10-23T08:16:40+00:00"     # 03:16:40 CDT


def run(manifests, since=DISPATCHED, attempts=3):
    """Run main() against a sequence of fetch results (dicts, or exceptions to raise)."""
    seen, sleeps = [], []

    def fetcher(url):
        seen.append(url)
        item = manifests[min(len(seen), len(manifests)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    rc = check_live_site.main(["--since", since, "--attempts", str(attempts), "--wait", "5"],
                              fetcher=fetcher, sleep=sleeps.append)
    return rc, seen, sleeps


def test_tonights_build_passes_first_time():
    rc, seen, sleeps = run([{"buildId": "20261023-081905", "dir": "d/20261023-081905"}])
    assert rc == 0 and len(seen) == 1 and sleeps == []


def test_yesterdays_build_fails_after_every_attempt(capsys):
    rc, seen, sleeps = run([{"buildId": "20261022-081311"}])
    assert rc == 1 and len(seen) == 3 and sleeps == [5, 5]
    assert "::error title=Live site is not tonight's build::" in capsys.readouterr().out


def test_a_build_from_earlier_the_same_chicago_day_but_before_the_dispatch_fails():
    """The dispatched build has to be the one on the site, not just any build from today."""
    assert run([{"buildId": "20261023-060000"}])[0] == 1


def test_a_cdn_still_serving_the_old_manifest_is_waited_out():
    rc, seen, _ = run([{"buildId": "20261022-081311"}, {"buildId": "20261023-081905"}])
    assert rc == 0 and len(seen) == 2


def test_a_build_that_ran_across_chicago_midnight_counts():
    # Dispatched 23:58 CDT on the 22nd, built 00:01 CDT on the 23rd.
    assert run([{"buildId": "20261023-050100"}], since="2026-10-23T04:58:00+00:00")[0] == 0


def test_a_little_clock_skew_between_runners_is_allowed():
    assert run([{"buildId": "20261023-081540"}])[0] == 0      # 60 s "before" the dispatch
    assert run([{"buildId": "20261023-081000"}])[0] == 1      # 6 min before is a different build


def test_network_errors_are_retried_then_reported(capsys):
    rc, seen, _ = run([OSError("timed out")])
    assert rc == 1 and len(seen) == 3
    assert "could not read" in capsys.readouterr().out
    assert run([OSError("timed out"), {"buildId": "20261023-081905"}])[0] == 0


def test_a_manifest_without_a_usable_build_id_fails():
    assert run([{"dir": "d/x"}])[0] == 1
    assert run([{"buildId": "tonight"}])[0] == 1
    assert run([["not", "an", "object"]])[0] == 1


def test_an_unusable_since_fails():
    assert run([{"buildId": "20261023-081905"}], since="2026-10-23T08:16:40")[0] == 1


def test_every_fetch_busts_the_cdn_cache(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"buildId": "20261023-081905"}'

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        return Response()

    monkeypatch.setattr(check_live_site.urllib.request, "urlopen", urlopen)
    assert check_live_site.fetch(check_live_site.URL) == {"buildId": "20261023-081905"}
    assert captured["url"].startswith(check_live_site.URL + "?nocache=")
    assert captured["headers"].get("Cache-control") == "no-cache"


def test_the_build_id_format_is_the_one_build_site_writes():
    """build_site.py stamps datetime.now() as %Y%m%d-%H%M%S; if that changes, change this too."""
    with open(build_site.__file__, encoding="utf-8") as f:
        assert 'build_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")' in f.read()
