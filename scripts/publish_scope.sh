#!/bin/bash
# Publish data/scope.jsonl as scope.jsonl.gz on the stable scope-data release,
# after checking it. Run by hand, from the checkout the nightly job runs in.
# See scripts/publish_release_asset.sh.
#
#     scripts/publish_scope.sh [--dry-run]
exec "$(dirname "$0")/publish_release_asset.sh" scope "$@"
