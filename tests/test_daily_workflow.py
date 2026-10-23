"""daily.yml's schedule, gate and scrape steps keep the shape the Oct 2026 audit gave them.

Read as text, because PyYAML is not in requirements.txt. Each test names the finding it
pins: sc-03/ci-07 (the DST gate that would have lost 14 Mar 2027), scraper-09 (buffered
logs, unbounded steps).
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, ".github", "workflows", "daily.yml"), encoding="utf-8") as f:
    DAILY = f.read()


def code(text):
    return "\n".join(line for line in text.splitlines()
                     if line.strip() and not line.lstrip().startswith("#"))


def job(job_id):
    """One job's lines, from its id line to the next job."""
    lines = DAILY.split("\njobs:\n", 1)[1].splitlines()
    start = lines.index("  %s:" % job_id)
    block = []
    for line in lines[start + 1:]:
        if re.match(r"^  \S", line):
            break
        block.append(line)
    return "\n".join(block)


def steps(job_id):
    blocks = []
    for line in job(job_id).splitlines():
        if re.match(r"^      - ", line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


def step(name, job_id="daily"):
    (found,) = [s for s in steps(job_id) if name in s.splitlines()[0]]
    return found


def index(name, job_id="daily"):
    (found,) = [i for i, s in enumerate(steps(job_id)) if name in s.splitlines()[0]]
    return found


CRONS = re.findall(r"- cron: '([^']+)'", DAILY)


def test_two_cron_entries_both_off_the_hour():
    assert len(CRONS) == 2
    for cron in CRONS:
        minute = cron.split()[0]
        assert minute.isdigit() and minute not in ("0", "00"), "%r fires on the hour" % cron


def test_the_gate_never_looks_at_which_entry_fired():
    gate = code(step("Decide whether tonight still needs a scrape"))
    assert "github.event.schedule" not in gate and "SCHEDULE" not in gate
    assert "TZ=America/Chicago date +%z" not in gate


def test_the_gate_skips_only_a_scheduled_run_whose_night_is_complete():
    gate = code(step("Decide whether tonight still needs a scrape"))
    assert "proceed=true\n" in gate, "the default must be to run"
    assert "if [ \"$EVENT\" = 'schedule' ]; then" in gate, "a manual dispatch always runs"
    assert 'if python3 scripts/nightly_gate.py --meta "$gate/scrape_meta.json"; then' in gate
    assert gate.count("proceed=false") == 1
    assert "ne-contracts-hub-data-" in gate and "scrape_meta.json.gz" in gate


def test_the_gate_reaches_github_only_through_env():
    gate = step("Decide whether tonight still needs a scrape")
    run = gate.split("run: |", 1)[1]
    assert "${{" not in run, "an expression inside run: is command injection"
    assert "GH_TOKEN: ${{ github.token }}" in gate


def test_the_gate_runs_before_setup_python_so_its_helpers_stay_stdlib_only():
    assert index("Decide whether tonight still needs a scrape") < index("actions/setup-python")
    for script in ("nightly_gate.py", "check_scrape_stamps.py"):
        with open(os.path.join(ROOT, "scripts", script), encoding="utf-8") as f:
            imports = re.findall(r"^\s*(?:import|from)\s+(\w+)", f.read(), re.M)
        assert set(imports) <= {"argparse", "datetime", "json", "os", "sys", "zoneinfo",
                                "check_scrape_stamps"}, (script, imports)


def test_logs_are_unbuffered():
    assert re.search(r"^    env:\n(?:      .*\n)*?      PYTHONUNBUFFERED: '1'", job("daily"), re.M)


def test_every_scrape_step_has_a_time_budget_that_fits_the_job():
    timeout = int(re.search(r"timeout-minutes: (\d+)", job("daily")).group(1))
    budgets = []
    for dataset in ("contract", "purchase-order", "state"):
        run = re.search(r"run: python3 scripts/scrape.py %s --daily(.*)" % re.escape(dataset),
                        step("Daily scrape -- " + dataset)).group(1)
        m = re.search(r"--hours ([\d.]+)", run)
        assert m, "%s has no --hours budget" % dataset
        budgets.append(float(m.group(1)) * 60)
    assert sum(budgets) <= timeout - 30, "the scrape budgets leave no room for the publish leg"


# --- the deploy dispatch (fresh-09) and the check that it reached the site (sc-01) ---------

def test_the_deploy_is_dispatched_only_after_the_cache_save_and_before_anything_that_can_fail():
    dispatch = index("Deploy the rebuilt site")
    assert index("Save data/ cache") < dispatch, "pages.yml restores the cache this run saves"
    assert index("Guard rail on tonight's diff") < dispatch
    for later in ("Delete old data/ cache entries", "Publish a data release for ne-connect",
                  "Delete old hub-data releases"):
        assert dispatch < index(later), "%r failing must not be able to skip the deploy" % later


def test_the_dispatch_needs_the_guard_rail_and_the_cache_save_to_have_succeeded():
    condition = " ".join(code(step("Deploy the rebuilt site")).split("if: >-", 1)[1]
                         .split("env:", 1)[0].split())
    for part in ("always()", "steps.gate.outputs.deploy == 'true'",
                 "steps.guard.outcome == 'success'", "steps.cachesave.outcome == 'success'"):
        assert part in condition, part
    assert "id: guard" in step("Guard rail on tonight's diff")
    assert "id: cachesave" in step("Save data/ cache")


def test_the_dispatch_time_leaves_the_job_as_an_output():
    assert "dispatched_at: ${{ steps.dispatch.outputs.dispatched_at }}" in job("daily")
    run = step("Deploy the rebuilt site")
    assert "dispatched=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" in run
    assert run.index("dispatched=$(date") < run.index("gh workflow run pages.yml")
    assert 'echo "dispatched_at=$dispatched" >> "$GITHUB_OUTPUT"' in run


def test_verify_deploy_waits_on_the_dispatched_run_and_reads_the_live_site():
    verify = job("verify-deploy")
    assert "    needs: daily" in verify
    assert "    if: always() && needs.daily.outputs.dispatched_at != ''" in verify
    found = step("Find the Pages run this night dispatched", "verify-deploy")
    assert "--workflow pages.yml --event workflow_dispatch" in found
    watch = step("Wait for it to build and deploy", "verify-deploy")
    assert "gh run watch" in watch and "--exit-status" in watch
    live = steps("verify-deploy")[-1]
    assert "Check the live site is tonight's build" in live.splitlines()[0]
    assert "if: always()" in live
    assert 'run: python3 scripts/check_live_site.py --since "$DISPATCHED_AT"' in live


def test_verify_deploy_can_only_read():
    verify = job("verify-deploy")
    perms = verify.split("    permissions:\n", 1)[1].split("\n    steps:", 1)[0]
    assert not [line for line in perms.splitlines() if "write" in line.split("#")[0]]
    assert "actions: read" in perms


def test_no_expression_is_interpolated_into_a_run_line():
    for job_id in ("daily", "verify-deploy"):
        for s in steps(job_id):
            if "run:" in s:
                assert "${{" not in s.split("run:", 1)[1], s.splitlines()[0]
