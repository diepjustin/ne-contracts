"""scripts/update_live_checkout.sh, against throwaway git repositories.

An "origin" and a "live" clone are made in a temporary directory. The live
clone's venv/bin/python3 is a stand-in that records pip and pytest calls and
fails them on request, handing anything else to the real Python. No network.
"""

import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
BASH = "/bin/bash"

pytestmark = pytest.mark.skipif(not (os.path.exists(BASH) and shutil.which("git")),
                                reason="needs /bin/bash and git")

FAKE_PYTHON = r"""#!/bin/bash
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  echo "pip $*" >> "$FAKE_LOG"; exit "${FAKE_PIP_EXIT:-0}"
fi
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  echo "pytest $(git rev-parse --short HEAD)" >> "$FAKE_LOG"; exit "${FAKE_PYTEST_EXIT:-0}"
fi
exec "$FAKE_REAL_PYTHON" "$@"
"""


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def live(tmp_path):
    seed = tmp_path / "seed"
    (seed / "scripts").mkdir(parents=True)
    (seed / "data").mkdir()
    (seed / "tests").mkdir()
    for name in ("update_live_checkout.sh", "ocr_io.py"):
        shutil.copy(os.path.join(SCRIPTS, name), seed / "scripts" / name)
    (seed / "requirements.txt").write_text("pypdf==6.18.0\n")
    (seed / "data" / "scrape_meta.json").write_text('{"stamp": "committed"}\n')
    (seed / ".gitignore").write_text("venv/\ndata/.ocr.lock\n")
    git(seed, "init", "-q")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "first")

    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    live = tmp_path / "live"
    git(tmp_path, "clone", "-q", str(origin), str(live))
    (live / "venv" / "bin").mkdir(parents=True)
    fake = live / "venv" / "bin" / "python3"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(0o755)
    # The nightly always leaves this modified.
    (live / "data" / "scrape_meta.json").write_text('{"stamp": "tonight"}\n')

    class Live:
        pass

    lv = Live()
    lv.path, lv.seed, lv.log = live, seed, tmp_path / "calls.log"
    lv.env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
              "FAKE_LOG": str(lv.log), "FAKE_REAL_PYTHON": sys.executable,
              "GIT_CONFIG_NOSYSTEM": "1"}

    def push(message, **files):
        for name, text in files.items():
            path = seed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", message)
        git(seed, "push", "-q", str(origin), "main")

    def run(**env):
        out = subprocess.run([BASH, str(live / "scripts" / "update_live_checkout.sh")],
                             env=dict(lv.env, **env), capture_output=True, text=True, timeout=120)
        lv.calls = lv.log.read_text().splitlines() if lv.log.exists() else []
        return out

    lv.push, lv.run = push, run
    lv.head = lambda: git(live, "rev-parse", "HEAD")
    lv.meta = lambda: (live / "data" / "scrape_meta.json").read_text()
    return lv


def test_fast_forwards_reinstalls_changed_requirements_and_runs_the_tests(live):
    live.push("bump pypdf", **{"requirements.txt": "pypdf==6.19.0\n"})
    out = live.run()
    assert out.returncode == 0, out.stdout + out.stderr
    assert live.head() == git(live.seed, "rev-parse", "HEAD")
    assert [c.split()[0] for c in live.calls] == ["pip", "pytest"]
    assert "-r requirements.txt" in live.calls[0]
    assert live.meta() == '{"stamp": "tonight"}\n'


def test_unchanged_requirements_are_not_reinstalled(live):
    live.push("docs", **{"README.md": "hello\n"})
    assert live.run().returncode == 0
    assert [c.split()[0] for c in live.calls] == ["pytest"]


def test_failing_tests_roll_back_and_keep_the_local_stamp(live):
    before = live.head()
    live.push("broken", **{"requirements.txt": "pypdf==6.19.0\n"})
    out = live.run(FAKE_PYTEST_EXIT="1")
    assert out.returncode == 1
    assert "rolling back" in out.stderr
    assert live.head() == before
    assert (live.path / "requirements.txt").read_text() == "pypdf==6.18.0\n"
    assert live.meta() == '{"stamp": "tonight"}\n'
    # Installed the new pins, then the old ones again.
    assert [c.split()[0] for c in live.calls] == ["pip", "pytest", "pip"]


def test_a_failed_install_rolls_back_without_running_the_tests(live):
    before = live.head()
    live.push("bad pin", **{"requirements.txt": "nonexistent==0\n"})
    out = live.run(FAKE_PIP_EXIT="1")
    assert out.returncode == 1
    assert live.head() == before
    assert not [c for c in live.calls if c.startswith("pytest")]


def test_local_commits_are_refused(live):
    git(live.path, "commit", "-q", "--allow-empty", "-m", "local work")
    before = live.head()
    live.push("upstream", **{"README.md": "x\n"})
    out = live.run()
    assert out.returncode == 1
    assert "local commit" in out.stderr
    assert live.head() == before
    assert live.calls == []


def test_other_local_changes_are_refused(live):
    (live.path / "requirements.txt").write_text("edited by hand\n")
    live.push("upstream", **{"README.md": "x\n"})
    out = live.run()
    assert out.returncode == 1
    assert "requirements.txt" in out.stderr


def test_already_up_to_date_does_nothing(live):
    out = live.run()
    assert out.returncode == 0
    assert "already at origin/main" in out.stdout
    assert live.calls == []


def test_refuses_while_the_ocr_job_holds_the_lock(live):
    sys.path.insert(0, SCRIPTS)
    import ocr_io

    live.push("upstream", **{"README.md": "x\n"})
    before = live.head()
    with ocr_io.run_lock(str(live.path / "data" / ".ocr.lock")):
        out = live.run()
    assert out.returncode == 75
    assert live.head() == before
