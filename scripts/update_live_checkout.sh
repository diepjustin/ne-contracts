#!/bin/bash
# Bring the checkout the nightly OCR job runs from (~/Developer/ne-contracts)
# up to date with origin/main, safely. Nothing did this before: the job ran
# whatever code was there when the checkout was copied.
#
#     scripts/update_live_checkout.sh
#
# What it does, and what makes it stop:
#   - refuses unless the checkout is on main with no local commits and no
#     changes to tracked files other than data/scrape_meta.json (which the
#     nightly rewrites every run, so it is always "modified");
#   - takes data/.ocr.lock, so it never updates code under a running job --
#     bash reads a script as it runs it, and rewriting run_ocr_catchup.sh
#     mid-run would corrupt the run;
#   - sets data/scrape_meta.json aside, fast-forwards (never merges), and puts
#     it back afterwards, whatever happened;
#   - reinstalls requirements.txt (and requirements-ocr.txt) into the venv
#     with `venv/bin/python3 -m pip` -- not venv/bin/pip, whose shebang can
#     name another copy's Python -- when either file changed;
#   - runs the test suite, and on any failure resets to the commit it started
#     from (reinstalling that commit's requirements if they had changed).
#
# Exit 0 when up to date or updated, 1 when refused or rolled back, 75 when
# the OCR job holds the lock.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$PATH:/opt/homebrew/bin"

PY="./venv/bin/python3"
META="data/scrape_meta.json"

die() { echo "update_live_checkout: $*" >&2; exit 1; }

case "$(pwd -P)" in
    *"/Library/Mobile Documents/"*) die "this is the iCloud copy; run it in ~/Developer/ne-contracts" ;;
esac
[ -x "$PY" ] || die "no venv at $PY"

branch=$(git symbolic-ref --short HEAD 2>/dev/null) || die "HEAD is detached"
[ "$branch" = "main" ] || die "on branch $branch, not main"

mkdir -p data
exec 9>>data/.ocr.lock
"$PY" scripts/ocr_io.py lock --fd 9
code=$?
if [ "$code" -eq 75 ]; then
    echo "update_live_checkout: the OCR job is running (data/.ocr.lock); try again later" >&2
    exit 75
elif [ "$code" -ne 0 ]; then
    die "could not take data/.ocr.lock (exit $code)"
fi

git fetch --quiet origin main || die "git fetch failed"

ahead=$(git rev-list --count origin/main..HEAD) || die "cannot compare with origin/main"
[ "$ahead" -eq 0 ] || die "$ahead local commit(s) not on origin/main; not touching them"

dirty=$(git status --porcelain --untracked-files=no | grep -v " $META\$" || true)
[ -z "$dirty" ] || die "tracked files have local changes besides $META:
$dirty"

prev=$(git rev-parse HEAD)
target=$(git rev-parse origin/main)
if [ "$prev" = "$target" ]; then
    echo "already at origin/main ($(git rev-parse --short HEAD))"
    exit 0
fi

saved=""
if ! git diff --quiet -- "$META" 2>/dev/null; then
    saved=$(mktemp "${TMPDIR:-/tmp}/scrape_meta.XXXXXX")
    cp "$META" "$saved" || die "could not set $META aside"
    git checkout -- "$META" || die "could not reset $META"
fi
restore_meta() {
    if [ -n "$saved" ]; then
        cp "$saved" "$META" && rm -f "$saved"
        saved=""
    fi
}
trap restore_meta EXIT

install_requirements() {
    "$PY" -m pip install --quiet -r requirements.txt || return 1
    if [ -f requirements-ocr.txt ]; then
        "$PY" -m pip install --quiet -r requirements-ocr.txt || return 1
    fi
}

git merge --ff-only --quiet origin/main || die "fast-forward to origin/main failed"
echo "fast-forwarded $(git rev-parse --short "$prev")..$(git rev-parse --short HEAD)"

reqs_changed=0
git diff --quiet "$prev" HEAD -- requirements.txt requirements-ocr.txt || reqs_changed=1

failed=""
if [ "$reqs_changed" = 1 ]; then
    echo "requirements changed; reinstalling into the venv"
    install_requirements || failed="pip install"
fi
if [ -z "$failed" ]; then
    "$PY" -m pytest -q tests/ || failed="the test suite"
fi

if [ -n "$failed" ]; then
    echo "update_live_checkout: $failed failed; rolling back to $(git rev-parse --short "$prev")" >&2
    git reset --hard --quiet "$prev"
    if [ "$reqs_changed" = 1 ]; then
        install_requirements || echo "update_live_checkout: reinstalling the old requirements failed too; check the venv" >&2
    fi
    exit 1
fi

echo "updated to $(git rev-parse --short HEAD); tests pass"
exit 0
