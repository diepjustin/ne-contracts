#!/bin/bash
# Publish the Mac's AI summaries or descriptions as a release asset. Run by hand,
# from the checkout the nightly job runs in (~/Developer/ne-contracts), after a
# good night:
#
#     scripts/publish_ai_summaries.sh            # data/ai_summaries.jsonl
#     scripts/publish_scope.sh                   # data/scope.jsonl
#     scripts/publish_ai_summaries.sh --dry-run  # check and compare, upload nothing
#
# Each goes to one stable release that never changes name, so a new dated
# release can no longer become "newest" without the asset and break pages.yml:
#
#     ai-summaries   tag ai-summaries-data   asset ai_summaries.jsonl.gz
#     scope          tag scope-data          asset scope.jsonl.gz
#
# Before uploading it:
#   - refuses to run from the iCloud copy of the repository, whose data/ is a
#     stale snapshot;
#   - takes data/.ocr.lock, so it never copies a file the nightly is writing,
#     and snapshots the file under that lock;
#   - checks the snapshot with scripts/check_release_asset.py: every line
#     parses, every summary's model is one build_site.AI_MODELS lists, and it
#     is a superset of what is published now (same or more records, no token
#     lost, no text lost). Until the stable release exists, "published now"
#     means the newest extraction-data-* release, which is what pages.yml
#     reads today.
#
# The upload never leaves the release without the asset for the length of an
# upload, which `gh release upload --clobber` does (it deletes first). The new
# file goes up as next-<asset>; then the old asset is renamed prev-<asset>, the
# new one renamed into place, and the old one deleted. A build that looks in
# the gap between the two renames -- one API call -- finds no asset; one that
# looks at any other moment finds a whole file, old or new. The release notes
# then record the record count and the sha256 of both the file and the .gz.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PATH:/opt/homebrew/bin"

REPO="diepjustin/ne-contracts"
KIND="${1:-}"
shift || true
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 64 ;;
    esac
done

case "$KIND" in
    ai-summaries)
        SRC="data/ai_summaries.jsonl"; TAG="ai-summaries-data"; ASSET="ai_summaries.jsonl.gz"
        TITLE="AI summaries (generated, unverified)"
        ABOUT="AI-generated one-sentence summaries of OCR'd documents, from scripts/generate_ai_summaries.py. Unverified: the site labels every one as such." ;;
    scope)
        SRC="data/scope.jsonl"; TAG="scope-data"; ASSET="scope.jsonl.gz"
        TITLE="Descriptions (scope.jsonl)"
        ABOUT="Descriptions lifted verbatim from documents' text layers by scripts/extract_scope.py." ;;
    *)
        echo "usage: $0 ai-summaries|scope [--dry-run]" >&2
        exit 64 ;;
esac

case "$(pwd -P)" in
    *"/Library/Mobile Documents/"*)
        echo "refusing to publish from the iCloud copy ($(pwd -P)): its data/ is not what" \
             "the nightly job writes. Run this from ~/Developer/ne-contracts." >&2
        exit 1 ;;
esac

if [ -x ./venv/bin/python3 ]; then PY=./venv/bin/python3; else PY=python3; fi

if [ ! -s "$SRC" ]; then
    echo "nothing to publish: $SRC is missing or empty" >&2
    exit 1
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/ne-publish.XXXXXX")
trap 'rm -rf "$work"' EXIT

# Snapshot under the lock, then let it go: the checks and the upload work on
# the copy, and the nightly may start meanwhile without harm.
exec 9>>data/.ocr.lock
if ! "$PY" scripts/ocr_io.py lock --fd 9; then
    echo "the OCR job (or another publish) holds data/.ocr.lock; try again once it finishes" >&2
    exit 75
fi
cp "$SRC" "$work/new.jsonl"
exec 9>&-

gh_missing() {
    # True when gh's stderr in $1 says the thing is not there, rather than
    # that gh could not ask (auth, network): only the first may be ignored.
    case "$1" in *"release not found"*|*"Not Found"*|*"HTTP 404"*|*"no assets match"*) return 0 ;; esac
    return 1
}

release_exists=0
if err=$(gh release view "$TAG" --repo "$REPO" 2>&1 >/dev/null); then
    release_exists=1
elif ! gh_missing "$err"; then
    echo "could not ask GitHub about $TAG: $err" >&2
    exit 1
fi

asset_id() {
    gh api "repos/$REPO/releases/tags/$TAG" --jq '.assets[] | "\(.id) \(.name)"' \
        | awk -v name="$1" '$2 == name { print $1 }'
}

# A publish that died between its two renames left the old asset as
# prev-<asset> and nothing under <asset>. That is the published copy, so put it
# back before anything else: left there, this run would find no baseline (and
# compare against an older extraction release, or nothing), and the leftovers
# cleanup below would delete the only real copy. A dry run changes nothing, so
# it compares against prev-<asset> where it lies.
recovered_prev=""
if [ "$release_exists" = 1 ] && [ -z "$(asset_id "$ASSET")" ]; then
    prev_id=$(asset_id "prev-$ASSET")
    if [ -n "$prev_id" ]; then
        if [ "$DRY_RUN" = 1 ]; then
            echo "$TAG has no $ASSET but has prev-$ASSET, left by a publish that died part" \
                 "way; a real run renames it back first" >&2
            recovered_prev="prev-$ASSET"
        elif gh api -X PATCH "repos/$REPO/releases/assets/$prev_id" -f name="$ASSET" >/dev/null; then
            echo "restored prev-$ASSET to $ASSET on $TAG, left by a publish that died part way" >&2
        else
            echo "$TAG has no $ASSET, only prev-$ASSET, and renaming it back failed;" \
                 "not publishing" >&2
            exit 1
        fi
    fi
fi

# The published copy to compare against: the stable release's, or until it
# has one, the newest extraction-data-* release's (pages.yml's own choice).
baseline_tag=""
mkdir -p "$work/old"
if [ "$release_exists" = 1 ]; then
    if [ -n "$recovered_prev" ]; then
        if ! err=$(gh release download "$TAG" --repo "$REPO" --pattern "$recovered_prev" --dir "$work/old" 2>&1 >/dev/null); then
            echo "could not download $recovered_prev from $TAG: $err" >&2
            exit 1
        fi
        mv "$work/old/$recovered_prev" "$work/old/$ASSET"
        baseline_tag="$TAG"
    elif err=$(gh release download "$TAG" --repo "$REPO" --pattern "$ASSET" --dir "$work/old" 2>&1 >/dev/null); then
        baseline_tag="$TAG"
    elif ! gh_missing "$err"; then
        echo "could not download the published $ASSET from $TAG: $err" >&2
        exit 1
    fi
fi
if [ -z "$baseline_tag" ]; then
    legacy=$(gh release list --repo "$REPO" --limit 50 --json tagName \
               --jq '[.[] | select(.tagName | startswith("extraction-data-"))] | sort_by(.tagName) | last | .tagName // ""')
    if [ -n "$legacy" ]; then
        if err=$(gh release download "$legacy" --repo "$REPO" --pattern "$ASSET" --dir "$work/old" 2>&1 >/dev/null); then
            baseline_tag="$legacy"
        elif ! gh_missing "$err"; then
            echo "could not download $ASSET from $legacy: $err" >&2
            exit 1
        fi
    fi
fi

check=("$PY" scripts/check_release_asset.py "$KIND" "$work/new.jsonl" --env)
if [ -n "$baseline_tag" ]; then
    gunzip -c "$work/old/$ASSET" > "$work/old.jsonl"
    check+=(--published "$work/old.jsonl")
    echo "comparing with the copy published on $baseline_tag"
else
    echo "no published $ASSET anywhere yet; nothing to compare against"
fi
if ! facts=$("${check[@]}"); then
    echo "not published." >&2
    exit 1
fi
eval "$facts"   # RECORDS, WITH_TEXT, TOKENS, SHA256 -- integers and hex only

gzip -n -c "$work/new.jsonl" > "$work/next-$ASSET"
GZ_SHA256=$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$work/next-$ASSET")

if [ "$DRY_RUN" = 1 ]; then
    echo "dry run: would publish $ASSET to $TAG ($RECORDS records, $WITH_TEXT with text," \
         "sha256 $SHA256, .gz sha256 $GZ_SHA256)"
    exit 0
fi

if [ "$release_exists" = 0 ]; then
    # --latest=false: this release is storage, not a version of anything.
    gh release create "$TAG" --repo "$REPO" --title "$TITLE" --latest=false \
        --notes "Being published by scripts/publish_release_asset.sh; the notes are filled in once the asset is up."
fi

# Leftovers from a publish that died part way. prev-<asset> is deleted only
# beside a <asset>: without one it is the published copy, never a leftover.
for stale in "next-$ASSET" "prev-$ASSET"; do
    id=$(asset_id "$stale")
    if [ -z "$id" ]; then continue; fi
    if [ "$stale" = "prev-$ASSET" ] && [ -z "$(asset_id "$ASSET")" ]; then
        echo "$TAG has prev-$ASSET but no $ASSET; not deleting it" >&2
        exit 1
    fi
    gh api -X DELETE "repos/$REPO/releases/assets/$id" >/dev/null
done

gh release upload "$TAG" "$work/next-$ASSET" --repo "$REPO"
new_id=$(asset_id "next-$ASSET")
old_id=$(asset_id "$ASSET")
if [ -z "$new_id" ]; then
    echo "uploaded next-$ASSET but cannot find it on $TAG; the published asset is untouched" >&2
    exit 1
fi

if [ -n "$old_id" ]; then
    gh api -X PATCH "repos/$REPO/releases/assets/$old_id" -f name="prev-$ASSET" >/dev/null
fi
if ! gh api -X PATCH "repos/$REPO/releases/assets/$new_id" -f name="$ASSET" >/dev/null; then
    echo "could not rename next-$ASSET into place; restoring the previous asset" >&2
    if [ -n "$old_id" ]; then
        gh api -X PATCH "repos/$REPO/releases/assets/$old_id" -f name="$ASSET" >/dev/null
    fi
    exit 1
fi
if [ -n "$old_id" ]; then
    gh api -X DELETE "repos/$REPO/releases/assets/$old_id" >/dev/null
fi

# Read it back: what is published now must be the file just checked.
mkdir -p "$work/verify"
gh release download "$TAG" --repo "$REPO" --pattern "$ASSET" --dir "$work/verify"
got=$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$work/verify/$ASSET")
if [ "$got" != "$GZ_SHA256" ]; then
    echo "published $ASSET reads back with sha256 $got, not $GZ_SHA256" >&2
    exit 1
fi

HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
gh release edit "$TAG" --repo "$REPO" --notes "$ABOUT

- asset: $ASSET
- records: $RECORDS ($WITH_TEXT with text, $TOKENS distinct tokens)
- sha256 of the uncompressed file: $SHA256
- sha256 of $ASSET: $GZ_SHA256
- published: $NOW, from commit $HEAD, compared against ${baseline_tag:-nothing (first publish)}

Published by scripts/publish_release_asset.sh. Replaced in place on every publish; the asset name never changes." >/dev/null

echo "published $ASSET to $TAG: $RECORDS records ($WITH_TEXT with text), sha256 $SHA256"
