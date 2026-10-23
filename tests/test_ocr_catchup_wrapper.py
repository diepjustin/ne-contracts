"""scripts/run_ocr_catchup.sh, run for real against stand-ins.

The wrapper runs on the Mac under /bin/bash 3.2, so these run it with /bin/bash
too. Everything it calls is replaced: venv/bin/python3 is a script that records
its arguments and plays each step (handing anything else to the real Python),
and gh, curl and ollama are stand-ins on a PATH that holds nothing else but
/usr/bin and /bin -- the wrapper appends Homebrew after them, so the real gh and
ollama are never reached. Nothing here touches the network.
"""

import datetime
import gzip
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import ocr_io  # noqa: E402

BASH = "/bin/bash"
pytestmark = pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")

HUB_FILES = ("nu_contracts.csv", "nu_purchase_orders.csv", "state_agencies.csv", "scrape_meta.json")

FAKE_PYTHON = r"""#!/bin/bash
echo "python $*" >> "$FAKE_LOG"
case "$1" in
  scripts/build_site.py)
    mkdir -p d/20261001-000000 d/20261023-043000
    echo '{"buildId": "20261023-043000", "dir": "d/20261023-043000"}' > manifest.json
    exit "${FAKE_BUILD_EXIT:-0}" ;;
  scripts/ocr_pilot.py)
    echo '{"tok": "new"}' >> data/ocr_pilot.jsonl
    exit "${FAKE_OCR_EXIT:-0}" ;;
  scripts/generate_ai_summaries.py)
    echo '{"tok": "new"}' >> data/ai_summaries.jsonl
    echo '{"tok": "new2"}' >> data/ai_summaries.jsonl
    exit "${FAKE_SUMMARY_EXIT:-0}" ;;
  scripts/extract_text.py) exit "${FAKE_TEXT_EXIT:-0}" ;;
  scripts/extract_scope.py) exit "${FAKE_SCOPE_EXIT:-0}" ;;
  scripts/scrape.py) exit "${FAKE_SCRAPE_EXIT:-0}" ;;
esac
exec "$FAKE_REAL_PYTHON" "$@"
"""

FAKE_GH = r"""#!/bin/bash
echo "gh $*" >> "$FAKE_LOG"
case "${FAKE_GH_MODE:-ok}" in
  missing) echo "release not found" >&2; exit 1 ;;
  network) echo "error connecting to api.github.com" >&2; exit 1 ;;
esac
dir=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--dir" ]; then dir="$2"; fi
  shift
done
cp "$FAKE_RELEASE_DIR"/*.gz "$dir"/
"""

FAKE_CURL = r"""#!/bin/bash
[ -e "$FAKE_OLLAMA_UP" ]
"""

# Records whether the lock's fd 9 reached the server: it must not, or the lock
# outlives the run.
FAKE_OLLAMA = r"""#!/bin/bash
echo "ollama $*" >> "$FAKE_LOG"
if { true >&9; } 2>/dev/null; then echo leaked > "$FAKE_OLLAMA_UP.fd9"; fi
touch "$FAKE_OLLAMA_UP"
echo "serving"
"""


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def job(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "logs").mkdir()
    for name in ("run_ocr_catchup.sh", "ocr_io.py"):
        shutil.copy(os.path.join(SCRIPTS, name), root / "scripts" / name)
    executable(root / "venv" / "bin" / "python3", FAKE_PYTHON)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable(bindir / "gh", FAKE_GH)
    executable(bindir / "curl", FAKE_CURL)
    executable(bindir / "ollama", FAKE_OLLAMA)

    release = tmp_path / "release"
    release.mkdir()
    for name in HUB_FILES:
        with gzip.open(release / (name + ".gz"), "wt") as f:
            f.write("fresh " + name + "\n")
    for name in HUB_FILES:
        (root / "data" / name).write_text("old " + name + "\n")

    class Job:
        pass

    j = Job()
    j.root, j.release, j.tmp = root, release, tmp_path
    j.log = tmp_path / "calls.log"
    j.env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(j.log),
        "FAKE_REAL_PYTHON": sys.executable,
        "FAKE_RELEASE_DIR": str(release),
        "FAKE_OLLAMA_UP": str(tmp_path / "ollama-up"),
    }

    def run(*args, **env):
        out = subprocess.run([BASH, str(root / "scripts" / "run_ocr_catchup.sh"), *args],
                             env=dict(j.env, **env), capture_output=True, text=True, timeout=120)
        j.calls = j.log.read_text().splitlines() if j.log.exists() else []
        return out

    def status():
        return json.loads((root / "logs" / "ocr_status.json").read_text())

    j.run, j.status = run, status
    return j


def steps(status):
    return {s["name"]: s["exit_code"] for s in status["steps"]}


def test_a_good_night_runs_every_step_and_exits_0(job):
    out = job.run()
    assert out.returncode == 0, out.stdout + out.stderr

    status = job.status()
    assert steps(status) == {"fetch": 0, "build": 0, "prune": 0, "extract_text": 0,
                             "extract_scope": 0, "ocr": 0, "ollama": 0, "summaries": 0}
    assert status["exit_code"] == 0 and status["finished"]
    assert status["source"] == "hub-release"
    assert status["new_ocr_records"] == 1 and status["new_summary_records"] == 2
    assert all(isinstance(s["seconds"], int) for s in status["steps"])

    for name in HUB_FILES:
        assert (job.root / "data" / name).read_text() == "fresh " + name + "\n"
    assert not (job.root / "data" / ".hub-download").exists()
    assert os.listdir(job.root / "d") == ["20261023-043000"]

    today = datetime.datetime.now(datetime.timezone.utc)
    gh = [c for c in job.calls if c.startswith("gh ")]
    assert len(gh) == 1 and "ne-contracts-hub-data-" in gh[0]
    # The Central date: equal to UTC's or one day behind it.
    tag_day = gh[0].split("ne-contracts-hub-data-")[1].split()[0]
    assert tag_day in {today.strftime("%Y-%m-%d"),
                       (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")}

    assert any(c.startswith("ollama serve") for c in job.calls)
    assert not os.path.exists(job.env["FAKE_OLLAMA_UP"] + ".fd9"), "the lock fd leaked into ollama"
    assert "serving" in (job.root / "logs" / "ollama_serve.log").read_text()
    # ...and the lock is free again once the run is over.
    with ocr_io.run_lock(str(job.root / "data" / ".ocr.lock")):
        pass


def test_the_ollama_log_is_appended_to_not_truncated(job):
    (job.root / "logs" / "ollama_serve.log").write_text("weeks of history\n")
    job.run()
    assert (job.root / "logs" / "ollama_serve.log").read_text().startswith("weeks of history\n")


def test_a_release_not_published_yet_exits_3_and_the_rest_still_runs(job):
    out = job.run(FAKE_GH_MODE="missing")
    assert out.returncode == 3
    assert "not published yet" in out.stdout
    assert steps(job.status())["fetch"] == 3
    assert steps(job.status())["summaries"] == 0
    for name in HUB_FILES:
        assert (job.root / "data" / name).read_text() == "old " + name + "\n"


def test_a_download_failure_is_not_mistaken_for_a_missing_release(job):
    assert job.run(FAKE_GH_MODE="network").returncode == 4


def test_an_incomplete_release_replaces_nothing(job):
    (job.release / "scrape_meta.json.gz").unlink()
    out = job.run()
    assert out.returncode == 4
    for name in HUB_FILES:
        assert (job.root / "data" / name).read_text() == "old " + name + "\n"


def test_a_corrupt_asset_replaces_nothing(job):
    (job.release / "state_agencies.csv.gz").write_bytes(b"\x1f\x8b not really gzip")
    assert job.run().returncode == 4
    assert (job.root / "data" / "nu_contracts.csv").read_text() == "old nu_contracts.csv\n"


def test_the_first_failing_code_is_the_exit_code_and_later_steps_still_run(job):
    out = job.run(FAKE_OCR_EXIT="2", FAKE_SUMMARY_EXIT="1")
    assert out.returncode == 2
    assert steps(job.status())["ocr"] == 2
    assert steps(job.status())["summaries"] == 1
    assert job.status()["exit_code"] == 2


def test_a_failed_build_is_never_pruned_after(job):
    out = job.run(FAKE_BUILD_EXIT="1")
    assert out.returncode == 1
    assert "prune" not in steps(job.status())
    assert sorted(os.listdir(job.root / "d")) == ["20261001-000000", "20261023-043000"]


def test_a_second_run_while_one_holds_the_lock_does_nothing(job):
    (job.root / "logs" / "ocr_status.json").write_text('{"the": "run in progress"}')
    with ocr_io.run_lock(str(job.root / "data" / ".ocr.lock")):
        out = job.run()
    assert out.returncode == 75
    assert [c for c in job.calls if not c.startswith("python scripts/ocr_io.py lock")] == []
    assert json.loads((job.root / "logs" / "ocr_status.json").read_text()) == {"the": "run in progress"}


def test_local_scrape_is_still_available_on_request(job):
    out = job.run("--local-scrape")
    assert out.returncode == 0, out.stdout + out.stderr
    assert not [c for c in job.calls if c.startswith("gh ")]
    assert [c for c in job.calls if "scrape.py" in c] == [
        "python scripts/scrape.py contract --daily",
        "python scripts/scrape.py purchase-order --daily",
        "python scripts/scrape.py state --daily"]
    assert job.status()["source"] == "local-scrape"


def test_dry_run_runs_nothing(job):
    out = job.run("--dry-run")
    assert out.returncode == 0
    assert job.calls == []
    assert "--- ocr:" in out.stdout
    assert not (job.root / "logs" / "ocr_status.json").exists()
