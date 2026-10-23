"""Download one asset from a release, telling "not published" apart from "download broke".

pages.yml fetches three files the Mac job and the backfills publish, none of which CI can
make: scope.jsonl.gz (descriptions), documents.jsonl.gz (document lists) and
ai_summaries.jsonl.gz. It used to fetch them with `gh release download ... 2>/dev/null`,
so a network or auth failure read exactly like "this release has no such asset", and the
site quietly published without descriptions or summaries (fresh-10). Here:

    python3 scripts/fetch_release_asset.py --repo diepjustin/ne-contracts --dir data \\
        --asset ai_summaries.jsonl.gz --tag ai-summaries-data \\
        --fallback-prefix extraction-data- --max-age-days 14

- Releases are listed once through the API. A tag that is not in the list, or a release
  without the asset, is **not found**: the next candidate is tried -- --tag first, then the
  newest release whose tag starts with --fallback-prefix -- and if none has it, a
  ::warning:: is printed and the exit status is 0 with no file written. The caller checks
  for the file.
- An asset missing from a release that exists is looked for once more after --retry-wait
  seconds before falling back, because publish_release_asset.sh renames the new asset
  into place with one API call between taking the old name away and giving it back.
- A failed download is retried once after --retry-wait seconds. Any other failure -- the
  listing, or the download twice, or a size that does not match the release's -- prints
  an ::error:: and exits 1, which fails the step.
- --max-age-days warns when the asset was last updated longer ago than that. The fixed
  tags are updated in place, so the asset's own updated_at is what counts, not the
  release's date.

Standard library only; `gh` does the HTTP and the authentication (GH_TOKEN).
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time

# One compact object per release, per line, across every page.
RELEASES_JQ = ".[] | {tag: .tag_name, assets: [.assets[] | {name, size, updated_at}]}"


class FetchError(Exception):
    pass


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def list_releases(repo):
    result = gh("api", "--paginate", f"repos/{repo}/releases?per_page=100", "--jq", RELEASES_JQ)
    if result.returncode != 0:
        raise FetchError(f"could not list the releases of {repo}: {(result.stderr or result.stdout).strip()}")
    try:
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except ValueError as e:
        raise FetchError(f"the release listing was not JSON: {e}") from e


def find(releases, tag, name):
    """(the release exists, its asset called `name` or None)."""
    for release in releases:
        if release.get("tag") == tag:
            for asset in release.get("assets") or []:
                if asset.get("name") == name:
                    return True, asset
            return True, None
    return False, None


def candidates(releases, tag, prefix):
    out = [tag] if tag else []
    if prefix:
        matching = sorted(r["tag"] for r in releases if str(r.get("tag", "")).startswith(prefix))
        if matching and matching[-1] not in out:
            out.append(matching[-1])
    return out


def age_days(asset, now):
    updated = datetime.datetime.fromisoformat(str(asset["updated_at"]).replace("Z", "+00:00"))
    return (now - updated).total_seconds() / 86400


def download(repo, tag, name, directory, size):
    """None on success, else why it failed."""
    dest = os.path.join(directory, name)
    if os.path.exists(dest):
        os.remove(dest)
    result = gh("release", "download", tag, "--repo", repo, "--pattern", name, "--dir", directory, "--clobber")
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or f"gh exited {result.returncode}"
    if not os.path.isfile(dest):
        return f"gh reported success but {dest} is not there"
    got = os.path.getsize(dest)
    if size is not None and got != size:
        return f"{dest} is {got} bytes, the release says {size}"
    return None


def main(argv=None, sleep=time.sleep, now=None):
    parser = argparse.ArgumentParser(description="Download a release asset; not-found warns, failure fails.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--asset", required=True)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--tag", default=None, help="the release to try first")
    parser.add_argument("--fallback-prefix", default=None, help="then the newest release whose tag starts with this")
    parser.add_argument("--max-age-days", type=float, default=0, help="warn if older (0: never)")
    parser.add_argument("--retry-wait", type=float, default=10)
    args = parser.parse_args(argv)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    name = args.asset

    try:
        os.makedirs(args.dir, exist_ok=True)
        releases = list_releases(args.repo)
        tried = candidates(releases, args.tag, args.fallback_prefix)
        for tag in tried:
            exists, asset = find(releases, tag, name)
            if not exists:
                print(f"{name}: no release {tag}")
                continue
            if asset is None:
                print(f"{name}: not on {tag} yet; looking again in {args.retry_wait:g} s "
                      "(a re-published asset is renamed into place in two steps)")
                sleep(args.retry_wait)
                releases = list_releases(args.repo)
                exists, asset = find(releases, tag, name)
                if asset is None:
                    print(f"{name}: not on {tag}")
                    continue

            why = download(args.repo, tag, name, args.dir, asset.get("size"))
            if why:
                print(f"{name}: download from {tag} failed ({why}); retrying in {args.retry_wait:g} s")
                sleep(args.retry_wait)
                _, again = find(list_releases(args.repo), tag, name)
                asset = again or asset
                why = download(args.repo, tag, name, args.dir, asset.get("size"))
            if why:
                print(f"::error title=Release download failed::{name} from {tag}: {why}")
                return 1

            age = age_days(asset, now)
            print(f"{name}: downloaded from {tag} (updated {asset['updated_at']}, {age:.1f} days ago)")
            if args.max_age_days and age > args.max_age_days:
                print(f"::warning title=Stale release asset::{name} on {tag} was last updated "
                      f"{asset['updated_at']}, {age:.0f} days ago (over {args.max_age_days:g}); "
                      "the site is publishing old data for it")
            return 0

        print(f"::warning title=Release asset not found::{name} is on none of: "
              f"{', '.join(tried) or 'no candidate release'}; building without it")
        return 0
    except FetchError as e:
        print(f"::error title=Release download failed::{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
