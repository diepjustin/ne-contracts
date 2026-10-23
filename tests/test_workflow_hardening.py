"""Every workflow keeps the supply-chain and shell hardening it was given in Oct 2026.

security-04, ci-11 and tests-12 in the audit: actions were pinned by moving major tags, every
job held the workflow's widest token, checkouts left a token in .git/config, and every run:
step was `bash -e` without pipefail, so a failing `gh ... | jq | xargs` prune passed silently.
These read the workflows as text (PyYAML is not in requirements.txt) and pin each property
for every file, including ones added later.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
NAMES = sorted(n for n in os.listdir(WORKFLOWS) if n.endswith((".yml", ".yaml")))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def code(text):
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def top_level_block(text, key):
    """The lines under a top-level key, up to the next top-level key."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(key + ":"):
            block = [line]
            for rest in lines[i + 1:]:
                if re.match(r"^\S", rest):
                    break
                block.append(rest)
            return block
    return None


def jobs(text):
    """{job id: its lines}."""
    body = text.split("\njobs:\n", 1)[1].splitlines()
    found, current = {}, None
    for line in body:
        m = re.match(r"^  ([\w-]+):\s*$", line)
        if m:
            current = m.group(1)
            found[current] = []
        elif re.match(r"^\S", line):
            break
        elif current:
            found[current].append(line)
    return found


def test_there_are_workflows_to_check():
    assert {"daily.yml", "pages.yml", "ci.yml"} <= set(NAMES)


def test_every_action_is_pinned_to_a_full_commit_sha_with_its_version():
    for name in NAMES:
        for line in code(read(".github", "workflows", name)):
            m = re.search(r"\buses:\s*(\S+)(.*)$", line)
            if not m:
                continue
            ref, rest = m.groups()
            if ref.startswith("./"):
                continue
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), \
                "%s: %r is not pinned to a 40-character commit SHA" % (name, ref)
            assert re.search(r"#\s*v\d+(\.\d+)*\b", rest), \
                "%s: %r has no '# vX.Y.Z' comment saying which release the SHA is" % (name, ref)


def test_the_same_action_is_pinned_to_the_same_sha_everywhere():
    seen = {}
    for name in NAMES:
        for action, sha in re.findall(r"uses:\s*([\w.-]+/[\w.-]+)(?:/[\w./-]+)?@([0-9a-f]{40})",
                                      read(".github", "workflows", name)):
            seen.setdefault(action, set()).add(sha)
    split = {a: s for a, s in seen.items() if len(s) > 1}
    assert not split, "one action pinned to different SHAs: %s" % split


def test_no_checkout_leaves_a_token_behind():
    for name in NAMES:
        lines = read(".github", "workflows", name).splitlines()
        for i, line in enumerate(lines):
            if "uses: actions/checkout@" in line:
                following = "\n".join(lines[i + 1:i + 4])
                assert "persist-credentials: false" in following, \
                    "%s line %d: checkout without persist-credentials: false" % (name, i + 1)


def test_every_run_step_is_bash_with_pipefail():
    for name in NAMES:
        text = read(".github", "workflows", name)
        block = top_level_block(text, "defaults")
        assert block and "    shell: bash" in block, \
            "%s: needs defaults.run.shell: bash (that is what turns on -o pipefail)" % name
        overrides = [line.strip() for line in code(text) if re.match(r"^\s+shell:", line)]
        assert set(overrides) <= {"shell: bash"}, "%s overrides the shell: %s" % (name, overrides)


def test_no_workflow_grants_write_at_workflow_level():
    for name in NAMES:
        block = top_level_block(read(".github", "workflows", name), "permissions")
        assert block, "%s has no top-level permissions block, so it gets the repository default" % name
        assert not [line for line in block if "write" in line.split("#")[0]], \
            "%s grants write at workflow level; move it to the job that needs it" % name


def test_every_job_in_a_workflow_without_global_permissions_declares_its_own():
    for name in NAMES:
        text = read(".github", "workflows", name)
        if top_level_block(text, "permissions")[0].strip() != "permissions: {}":
            continue
        for job, lines in jobs(text).items():
            assert "    permissions:" in lines, "%s job %s declares no permissions" % (name, job)


def test_only_the_pages_deploy_job_can_deploy():
    pages = jobs(read(".github", "workflows", "pages.yml"))
    build = "\n".join(pages["build"])
    deploy = "\n".join(pages["deploy"])
    assert "pages: write" not in build and "id-token" not in build, \
        "the build job runs pip and npm; it must not hold the deploy credentials"
    assert "pages: write" in deploy and "id-token: write" in deploy
    assert len([line for line in code(deploy) if "uses:" in line]) == 1, \
        "the job holding pages: write should run one pinned action and nothing else"
    assert "run:" not in deploy


def test_dependabot_watches_the_actions_and_the_python_requirements():
    text = read(".github", "dependabot.yml")
    assert "version: 2" in text
    for ecosystem in ("github-actions", "pip"):
        assert "package-ecosystem: %s" % ecosystem in text
    assert text.count("interval: weekly") == 2


def test_the_page_lint_runs_a_pinned_eslint():
    """The npx call in lint_page.py names an exact version, never a bare `eslint`."""
    assert re.search(r'ESLINT = "eslint@\d+\.\d+\.\d+"', read("scripts", "lint_page.py"))


def test_the_keepalive_enables_the_nightly_and_itself_with_nothing_more_than_actions_write():
    """ci-08: a scheduled workflow GitHub has auto-disabled never runs again on its own."""
    text = read(".github", "workflows", "keepalive.yml")
    assert re.search(r"- cron: '[^']+'", text) and "workflow_dispatch:" in text
    assert "for workflow in daily.yml keepalive.yml; do" in text
    assert 'gh api -X PUT "repos/$GITHUB_REPOSITORY/actions/workflows/$workflow/enable"' in text
    perms = [line.split("#")[0].strip() for line in jobs(text)["keepalive"] if ": " in line]
    assert "actions: write" in perms
    assert not [p for p in perms if p.endswith(("write", "read")) and p != "actions: write"]
    assert "actions/checkout" not in text, "it needs no code, so it should not fetch any"
