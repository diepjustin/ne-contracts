"""seed-cache.yml seeds the nightly's cache only from a source the nightly can trust.

The cache it saves is the baseline every --daily run diffs against, so whatever it seeds
from is what the site reports next. Read as text, because PyYAML is not in
requirements.txt. Pins the 26 Oct 2026 audit's n19 (a way back from the newest hub-data
release once every cache entry is gone) and n21 (an artifact from any run was accepted).
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, ".github", "workflows", "seed-cache.yml"), encoding="utf-8") as f:
    SEED = f.read()


def steps():
    blocks = []
    for line in SEED.split("\njobs:\n", 1)[1].splitlines():
        if re.match(r"^      - ", line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


def step(name):
    (found,) = [s for s in steps() if name in s.splitlines()[0]]
    return found


def position(name):
    (found,) = [i for i, s in enumerate(steps()) if name in s.splitlines()[0]]
    return found


def test_an_artifact_is_taken_only_from_a_rescue_cache_run_of_this_repository():
    check = step("Check the run is a rescue-cache.yml run of this repository")
    assert position("Check the run is a rescue-cache.yml run") < position("Download data/ from the rescue run")
    assert "--jq '[.path, .event, .head_repository.full_name, .conclusion] | join(\" \")'" in check
    assert 'want=".github/workflows/rescue-cache.yml workflow_dispatch $GITHUB_REPOSITORY success"' in check
    assert "*[!0-9]*" in check, "the run id is checked to be a number before it reaches a URL"


def test_the_release_path_takes_the_newest_hub_data_release_by_tag_name():
    release = step("Download data/ from the newest hub-data release")
    assert "if: inputs.from_release" in release
    assert 'select(startswith("ne-contracts-hub-data-"))] | sort | last' in release
    assert "createdAt" not in release, "createdAt ties across nights; sort by tag name"
    assert 'tar -xzf "$dl/daily_state.tar.gz" -C data' in release
    assert position("Download data/ from the newest hub-data release") \
        < position("Verify data/ carries the CSVs before seeding") < position("Save the cache under this ref")


def test_two_sources_at_once_are_refused():
    both = step("Take one source at a time")
    assert "if: inputs.artifact_run_id != '' && inputs.from_release" in both
    assert "exit 1" in both
    assert position("Take one source at a time") == 1, "right after the checkout, before anything downloads"


def test_inputs_reach_the_shell_only_through_env():
    for s in steps():
        if "run:" in s:
            assert "${{" not in s.split("run:", 1)[1], s.splitlines()[0]
