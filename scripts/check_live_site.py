"""Is the live site tonight's build? The last check of the nightly, against the site itself.

daily.yml dispatches pages.yml and, until Oct 2026, never looked back: a Pages build that
failed left the site frozen while the nightly stayed green (sc-01). The verify-deploy job
now waits on that dispatched run, then runs this against the published manifest.json:

    python3 scripts/check_live_site.py --since 2026-10-23T08:16:40+00:00

It passes only when the live manifest's buildId was stamped at or after --since (the moment
the nightly dispatched the build, less a little clock slack), so the build on the site is
the one this night asked for -- which also makes it today's build in America/Chicago, or
the night's, if the build ran across midnight. A stale manifest is retried for a while,
because Pages serves through a CDN that can hold the old file for minutes after a deploy.

build_site.py stamps buildId with `datetime.now()` on the runner, whose clock is UTC, as
YYYYMMDD-HHMMSS; this reads it as UTC.

Exit 0: the live build is fresh. Exit 1, with an ::error line: it is not, or the manifest
could not be read. Standard library only.
"""
import argparse
import datetime
import json
import sys
import time
import urllib.request

URL = "https://diepjustin.github.io/ne-contracts/manifest.json"
USER_AGENT = "ne-contracts nightly deploy check (+https://github.com/diepjustin/ne-contracts)"
SLACK = datetime.timedelta(minutes=2)
TITLE = "Live site is not tonight's build"


def parse_time(text):
    moment = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if moment.utcoffset() is None:
        raise ValueError(f"{text!r} has no UTC offset")
    return moment


def build_time(build_id):
    """The moment a buildId (YYYYMMDD-HHMMSS, runner time, which is UTC) was stamped."""
    return datetime.datetime.strptime(str(build_id), "%Y%m%d-%H%M%S").replace(tzinfo=datetime.timezone.utc)


def chicago_date(moment):
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("America/Chicago")).date().isoformat()
    except Exception:  # noqa: BLE001 -- informational only
        return "unknown"


def verdict(manifest, since):
    """(ok, message) for one fetched manifest."""
    if not isinstance(manifest, dict) or "buildId" not in manifest:
        return False, "manifest.json has no buildId"
    try:
        built = build_time(manifest["buildId"])
    except ValueError:
        return False, f"buildId {str(manifest['buildId'])[:40]!r} is not YYYYMMDD-HHMMSS"
    where = f"buildId {manifest['buildId']} (built {built.isoformat()}, {chicago_date(built)} in Chicago)"
    if built >= since - SLACK:
        return True, f"{where} is at or after the dispatch at {since.isoformat()}"
    return False, f"{where} is older than the dispatch at {since.isoformat()}"


def fetch(url, timeout=30):
    # A query string the CDN has never seen, plus no-cache, so this reads what Pages now serves.
    busted = f"{url}{'&' if '?' in url else '?'}nocache={int(time.time())}"
    request = urllib.request.Request(busted, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv=None, fetcher=fetch, sleep=time.sleep):
    parser = argparse.ArgumentParser(description="Fail unless the live manifest is a build from after --since.")
    parser.add_argument("--since", required=True, help="when the build was dispatched, ISO-8601 with an offset")
    parser.add_argument("--url", default=URL)
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--wait", type=float, default=60, help="seconds between attempts")
    args = parser.parse_args(argv)

    try:
        since = parse_time(args.since)
    except ValueError as e:
        print(f"::error title={TITLE}::--since is not usable: {e}")
        return 1

    message = "never fetched"
    for attempt in range(1, max(1, args.attempts) + 1):
        try:
            ok, message = verdict(fetcher(args.url), since)
        except Exception as e:  # noqa: BLE001 -- network and JSON errors are retried, then reported
            ok, message = False, f"could not read {args.url}: {type(e).__name__}: {e}"
        print(f"attempt {attempt}: {message}")
        if ok:
            return 0
        if attempt < args.attempts:
            sleep(args.wait)
    print(f"::error title={TITLE}::{message}".replace("\n", " "))
    return 1


if __name__ == "__main__":
    sys.exit(main())
