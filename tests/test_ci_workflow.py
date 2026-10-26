"""A pull request can produce the status check that branch protection requires.

Protection on main requires a check named "build" and, until ci.yml, the only job with that
name was pages.yml's, which runs on a push to main -- after a change has landed. No pull
request could ever satisfy it, so PR #1 sat BLOCKED with no checks at all, and nothing tested
a change before it merged. These read the workflows as text, because PyYAML is not in
requirements.txt, and pin the three things that make the check producible and safe: the
trigger, the job id, and that a PR run cannot publish or touch a secret.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The status check branch protection on main requires (Settings > Branches). If that changes,
# change this and the job id in ci.yml together.
REQUIRED_CONTEXT = "build"


def workflow(name):
    with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as f:
        return f.read()


def code_lines(text):
    """The lines of a workflow with comments and blanks removed."""
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


CI = "\n".join(code_lines(workflow("ci.yml")))     # code only: its comments name what it must not do


def job_block(text, job_id):
    """The lines belonging to one job, from its id line to the next job or the end."""
    lines = text.splitlines()
    start = lines.index("  %s:" % job_id)
    block = []
    for line in lines[start + 1:]:
        if re.match(r"^  \S", line):
            break
        block.append(line)
    return block


def test_a_pull_request_to_main_triggers_it():
    on = CI[CI.index("on:"):].split("permissions:")[0]
    assert "  pull_request:" in on and "branches: [main]" in on
    assert "  push:" not in on, "a push trigger would report the same check twice on a PR's head commit"


def test_it_never_runs_with_the_base_repositorys_privileges():
    assert "pull_request_target" not in CI
    assert "secrets." not in CI and "GH_TOKEN" not in CI and "id-token" not in CI


def test_it_reports_the_required_context_and_nothing_renames_it():
    jobs = [m.group(1) for m in re.finditer(r"^  (\S+):\s*$", CI.split("\njobs:\n", 1)[1], re.M)]
    assert jobs == [REQUIRED_CONTEXT]
    assert not [line for line in job_block(CI, REQUIRED_CONTEXT) if re.match(r"^    name:", line)], \
        "a job-level name: would rename the check"


def test_it_can_read_the_repository_and_do_nothing_else():
    lines = CI.splitlines()
    assert lines[lines.index("permissions:") + 1] == "  contents: read"
    forbidden = ("pages: write", "contents: write", "actions: write", "actions/cache@",
                 "actions/cache/save", "gh release", "gh cache", "gh workflow",
                 "upload-pages-artifact", "deploy-pages")
    assert not [word for word in forbidden if word in CI]


def test_the_only_cache_it_touches_it_restores():
    """The page check reads the payload pages.yml built on main. Restoring a base-branch
    cache is read-only; saving one from a PR would hand the next run whatever the PR built."""
    uses = re.findall(r"uses: (actions/cache\S*)@", CI)
    assert uses == ["actions/cache/restore"]


def test_it_runs_the_tests_the_page_lint_and_the_page_check_the_publish_job_runs():
    pages = "\n".join(code_lines(workflow("pages.yml")))
    for command in ("python3 -m pytest tests/ -q", "python3 scripts/lint_page.py", "pip install -r requirements.txt",
                    "playwright@1.63.0 axe-core@4.10.2", 'playwright" install --with-deps chromium'):
        assert command in CI and command in pages, command
    assert 'node scripts/check_page.cjs _site' in pages
    assert 'node scripts/check_page.cjs .' in CI


def test_the_page_check_runs_on_the_staged_site_before_the_upload():
    pages = workflow("pages.yml")
    assert pages.index("- name: Stage the site") < pages.index("- name: Check the staged page in a browser") \
        < pages.index("uses: actions/upload-pages-artifact")


def test_the_publish_workflow_still_does_not_run_on_pull_requests():
    """Its deploy and cache-prune steps would run on PR refs."""
    for name in ("pages.yml", "daily.yml", "rescue-cache.yml", "seed-cache.yml", "keepalive.yml"):
        assert "pull_request" not in "\n".join(code_lines(workflow(name))), name
