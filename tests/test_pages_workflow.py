"""pages.yml fetches what CI cannot make without hiding failures, and publishes only the site.

Read as text, because PyYAML is not in requirements.txt. fresh-10 (download errors hidden
behind 2>/dev/null) and docs-16 (scripts/, tests/ and the README deployed as site content).
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, ".github", "workflows", "pages.yml"), encoding="utf-8") as f:
    PAGES = f.read()


def code(text):
    return "\n".join(line for line in text.splitlines()
                     if line.strip() and not line.lstrip().startswith("#"))


def steps():
    blocks = []
    for line in PAGES.split("\njobs:\n", 1)[1].splitlines():
        if re.match(r"^      - ", line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return ["\n".join(b) for b in blocks]


def step(name):
    (found,) = [s for s in steps() if name in s.splitlines()[0]]
    return code(found)


FETCH = step("Fetch descriptions, document lists, and AI summaries")


def test_no_error_is_thrown_away():
    assert "2>/dev/null" not in code(PAGES)


def test_each_asset_comes_from_its_fixed_release_then_the_extraction_release():
    assert "--fallback-prefix extraction-data-" in FETCH
    assert "fetch --asset scope.jsonl.gz --tag scope-data --max-age-days 14" in FETCH
    assert "fetch --asset ai_summaries.jsonl.gz --tag ai-summaries-data --max-age-days 14" in FETCH
    assert re.search(r"^\s*fetch --asset documents\.jsonl\.gz$", FETCH, re.M), \
        "documents.jsonl.gz stays on extraction-data-* only"
    assert "python3 scripts/fetch_release_asset.py" in FETCH


def test_the_document_log_is_still_merged_cached_first():
    assert 'cat "$keep" "$cached" > data/documents.merged.jsonl' in FETCH
    assert FETCH.index('mv "$cached" "$keep"') < FETCH.index("gunzip -f data/documents.jsonl.gz"), \
        "gunzip writes data/documents.jsonl, so the cached copy has to move out of the way first"
    assert "sed -i '$d' \"$keep\"" in FETCH, "a torn last line in the cached log is dropped"


def test_every_asset_is_used_only_if_it_was_downloaded_now():
    for name in ("scope.jsonl.gz", "documents.jsonl.gz", "ai_summaries.jsonl.gz"):
        assert "if [ -f data/%s ]; then" % name in FETCH
