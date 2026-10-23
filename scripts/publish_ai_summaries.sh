#!/bin/bash
# Publish data/ai_summaries.jsonl as ai_summaries.jsonl.gz on the stable
# ai-summaries-data release, after checking it. Run by hand, from the checkout
# the nightly job runs in. See scripts/publish_release_asset.sh.
#
#     scripts/publish_ai_summaries.sh [--dry-run]
exec "$(dirname "$0")/publish_release_asset.sh" ai-summaries "$@"
