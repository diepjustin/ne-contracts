"""daily.yml still runs the freshness check, after everything it must not block, with what it needs.

An alarm that can be dropped without anyone noticing is not one: the next tidy-up of daily.yml
could delete the last step, or the line that records when the run began, and the nightly would
go back to ending green on a night that did not finish. These read the workflow as text, because
PyYAML is not in requirements.txt, and pin only what the alarm depends on -- the step, its
position, how the start time reaches it, and the format of that time. They fail loudly in the
Tests step, before the scrape, with the reason in the assertion message.
"""

import datetime
import os
import re
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import check_scrape_stamps  # noqa: E402

ROOT = os.path.dirname(SCRIPTS)
with open(os.path.join(ROOT, ".github", "workflows", "daily.yml"), encoding="utf-8") as f:
    DAILY = f.read()


def steps(text):
    """The job's steps as blocks of text, in order. A step starts at a line that is six spaces
    and a dash, which is how this file indents them; comments above a step travel with the
    step before it, which is harmless here."""
    blocks = []
    for line in text.splitlines():
        if re.match(r"^      - ", line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


STEPS = steps(DAILY)


def position(fragment):
    found = [i for i, block in enumerate(STEPS) if fragment in block.splitlines()[0]]
    assert len(found) == 1, "expected exactly one step whose first line holds %r, found %d" % (fragment, len(found))
    return found[0]


def line_starting(block, prefix):
    (found,) = [line.strip() for line in block.splitlines() if line.strip().startswith(prefix)]
    return found


def test_the_steps_are_found_at_all():
    assert len(STEPS) >= 15, "the step splitter no longer recognises this file's indentation"
    assert STEPS[0].startswith("      - uses: actions/checkout")


def test_the_check_is_the_last_step_and_runs_the_script():
    last = STEPS[-1]
    assert "Check that every dataset finished tonight" in last.splitlines()[0], (
        "the freshness check must stay the LAST step of daily.yml, so it can colour the run red "
        "without blocking anything; see scripts/check_scrape_stamps.py")
    assert line_starting(last, "run:") == 'run: python3 scripts/check_scrape_stamps.py --since "$RUN_STARTED"'


def test_it_comes_after_everything_it_must_not_block():
    check = position("Check that every dataset finished tonight")
    for name in ("Deploy the rebuilt site", "Save data/ cache", "Publish a data release for ne-connect",
                 "Delete old hub-data releases"):
        assert position(name) < check, "%r must run before the freshness check, not after it" % name


def test_the_start_time_reaches_it_through_env_never_through_the_command_line():
    last = STEPS[-1]
    assert "RUN_STARTED: ${{ steps.gate.outputs.started }}" in last
    assert "${{" not in line_starting(last, "run:"), "an expression in a run: line is command injection"


def test_it_runs_on_a_bad_night_and_depends_on_no_step_id_but_the_gate():
    condition = line_starting(STEPS[-1], "if:")
    assert condition == "if: always() && steps.gate.outputs.proceed == 'true'"
    assert set(re.findall(r"steps\.(\w+)", condition)) == {"gate"}


def test_the_gate_step_records_when_the_run_began():
    gate = STEPS[position("Decide whether tonight still needs a scrape")]
    assert "id: gate" in gate
    assert "started=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" in gate
    assert 'echo "started=$started" >> "$GITHUB_OUTPUT"' in gate


def test_the_format_the_gate_writes_is_one_the_check_reads():
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    written = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")        # what `date -u +%Y-%m-%dT%H:%M:%S+00:00` prints
    assert check_scrape_stamps.parse_time(written) == now
