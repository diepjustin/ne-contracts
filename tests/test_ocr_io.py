"""The OCR job's shared plumbing: torn lines, appends, the run lock, the exit rule.

ocr_pilot.jsonl is 1.2 GB and is appended to by one script while another reads
it, and the job ran for weeks with no lock against a second copy of itself. None
of this touches the network.
"""

import json
import os
import subprocess
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import ocr_io  # noqa: E402


def write(path, data):
    path.write_bytes(data)
    return str(path)


# --- iter_jsonl ----------------------------------------------------------------------------

def test_complete_lines_load_and_blank_ones_are_skipped(tmp_path):
    path = write(tmp_path / "a.jsonl", b'{"a": 1}\n\n  \n{"a": 2}\n')
    assert [r for _, r in ocr_io.iter_jsonl(path)] == [{"a": 1}, {"a": 2}]


def test_a_final_line_without_a_newline_is_torn_even_when_it_parses(tmp_path):
    torn = []
    path = write(tmp_path / "a.jsonl", b'{"a": 1}\n{"a": 2}')
    records = [r for _, r in ocr_io.iter_jsonl(path, on_torn=lambda n, b: torn.append(n))]
    assert records == [{"a": 1}]
    assert torn == [2]


def test_reading_never_truncates_the_file(tmp_path):
    data = b'{"a": 1}\n{"a": 2, "b": "cut'
    path = write(tmp_path / "a.jsonl", data)
    list(ocr_io.iter_jsonl(path))
    assert (tmp_path / "a.jsonl").read_bytes() == data


def test_a_bad_line_with_more_after_it_raises(tmp_path):
    path = write(tmp_path / "a.jsonl", b'{"a": 1}\n{not json}\n{"a": 2}\n')
    with pytest.raises(ValueError, match="line 2"):
        list(ocr_io.iter_jsonl(path))


def test_a_unicode_line_separator_inside_a_string_is_not_a_line_break(tmp_path):
    record = {"text": "one two\u0085three"}
    path = write(tmp_path / "a.jsonl", (json.dumps(record, ensure_ascii=False) + "\n").encode())
    assert [r for _, r in ocr_io.iter_jsonl(path)] == [record]


# --- append_jsonl --------------------------------------------------------------------------

def test_append_after_a_tail_missing_its_newline_starts_a_new_line(tmp_path):
    path = write(tmp_path / "a.jsonl", b'{"a": 1}')
    ocr_io.append_jsonl(path, [{"a": 2}])
    assert (tmp_path / "a.jsonl").read_bytes() == b'{"a": 1}\n{"a": 2}\n'


def test_append_to_a_clean_or_missing_file_adds_no_blank_line(tmp_path):
    path = str(tmp_path / "new" / "a.jsonl")
    ocr_io.append_jsonl(path, [{"a": 1}])
    ocr_io.append_jsonl(path, [{"a": 2}, {"a": 3}])
    ocr_io.append_jsonl(path, [])
    with open(path, "rb") as f:
        assert f.read() == b'{"a": 1}\n{"a": 2}\n{"a": 3}\n'


# --- atomic_write --------------------------------------------------------------------------

def test_atomic_write_leaves_the_old_file_when_the_writer_fails(tmp_path):
    path = tmp_path / "out.jsonl"
    path.write_text("old\n")
    with pytest.raises(RuntimeError):
        with ocr_io.atomic_write(str(path)) as f:
            f.write("half")
            raise RuntimeError("killed")
    assert path.read_text() == "old\n"
    assert os.listdir(tmp_path) == ["out.jsonl"]


def test_atomic_write_replaces_the_file(tmp_path):
    path = tmp_path / "out.jsonl"
    path.write_text("old\n")
    with ocr_io.atomic_write(str(path)) as f:
        f.write("new\n")
    assert path.read_text() == "new\n"
    assert os.listdir(tmp_path) == ["out.jsonl"]


# --- the run lock --------------------------------------------------------------------------

HOLD = """
import sys, time
sys.path.insert(0, {scripts!r})
import ocr_io
with ocr_io.run_lock({path!r}):
    print("held", flush=True)
    sys.stdin.readline()
"""


def test_a_second_run_cannot_take_the_lock_while_the_first_holds_it(tmp_path, monkeypatch):
    monkeypatch.delenv(ocr_io.LOCK_FD_ENV, raising=False)
    path = str(tmp_path / ".ocr.lock")
    holder = subprocess.Popen([sys.executable, "-c", HOLD.format(scripts=SCRIPTS, path=path)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(ocr_io.LockBusy):
            with ocr_io.run_lock(path):
                pass
    finally:
        holder.communicate("\n")
    with ocr_io.run_lock(path):  # released once the holder exits
        pass


def test_a_child_of_the_lock_holder_reuses_the_inherited_lock(tmp_path):
    """The wrapper's own steps must not be locked out by the wrapper."""
    path = str(tmp_path / ".ocr.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        ocr_io.lock_fd(fd)
        child = (f"import sys; sys.path.insert(0, {SCRIPTS!r}); import ocr_io\n"
                 f"with ocr_io.run_lock({path!r}): print('ok')")
        env = dict(os.environ, **{ocr_io.LOCK_FD_ENV: str(fd)})
        out = subprocess.run([sys.executable, "-c", child], env=env, pass_fds=(fd,),
                             capture_output=True, text=True)
        assert out.stdout.strip() == "ok", out.stderr
        # ...and the child did not release the parent's lock on its way out.
        other = os.open(path, os.O_RDWR)
        try:
            with pytest.raises(ocr_io.LockBusy):
                ocr_io.lock_fd(other)
        finally:
            os.close(other)
    finally:
        os.close(fd)


def test_a_leaked_fd_variable_that_names_another_file_is_not_trusted(tmp_path, monkeypatch):
    path = str(tmp_path / ".ocr.lock")
    unrelated = os.open(str(tmp_path / "other"), os.O_RDWR | os.O_CREAT)
    holder = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        ocr_io.lock_fd(holder)
        monkeypatch.setenv(ocr_io.LOCK_FD_ENV, str(unrelated))
        with pytest.raises(ocr_io.LockBusy):
            with ocr_io.run_lock(path):
                pass
    finally:
        os.close(holder)
        os.close(unrelated)


def test_the_lock_command_exits_75_when_busy(tmp_path):
    path = str(tmp_path / ".ocr.lock")
    holder = os.open(path, os.O_RDWR | os.O_CREAT)
    other = os.open(path, os.O_RDWR)
    try:
        ocr_io.lock_fd(holder)
        out = subprocess.run([sys.executable, os.path.join(SCRIPTS, "ocr_io.py"), "lock",
                              "--fd", str(other)], pass_fds=(other,), capture_output=True)
        assert out.returncode == ocr_io.LOCK_BUSY_EXIT
    finally:
        os.close(holder)
        os.close(other)


# --- the exit rule -------------------------------------------------------------------------

@pytest.mark.parametrize("errors,total,code", [
    (0, 0, 0),      # nothing to do is a success
    (0, 10, 0),
    (5, 10, 0),     # exactly half is not "more than half"
    (6, 10, 2),
    (1, 1, 2),
])
def test_more_than_half_failed_exits_2(errors, total, code):
    assert ocr_io.failed_run_exit(errors, total) == code


# --- prune-builds --------------------------------------------------------------------------

def make_builds(root, names, current):
    for name in names:
        os.makedirs(os.path.join(root, "d", name))
    with open(os.path.join(root, "manifest.json"), "w") as f:
        json.dump({"buildId": current, "dir": f"d/{current}"}, f)


def test_prune_keeps_only_the_build_manifest_names(tmp_path):
    root = str(tmp_path)
    make_builds(root, ["20261020-010101", "20261021-010101", "20261022-010101"], "20261021-010101")
    os.makedirs(os.path.join(root, "d", "keep-me"))  # not a build id: never touched
    assert ocr_io.prune_builds(root) == ["20261020-010101", "20261022-010101"]
    assert sorted(os.listdir(os.path.join(root, "d"))) == ["20261021-010101", "keep-me"]


@pytest.mark.parametrize("manifest", [None, "{not json", '{"dir": "../etc"}', '{"dir": "d/20990101-000000"}'])
def test_prune_refuses_without_a_good_current_build(tmp_path, manifest):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "d", "20261020-010101"))
    if manifest is not None:
        (tmp_path / "manifest.json").write_text(manifest)
    with pytest.raises(ValueError):
        ocr_io.prune_builds(root)
    assert os.listdir(os.path.join(root, "d")) == ["20261020-010101"]


# --- status --------------------------------------------------------------------------------

def test_status_command_writes_the_heartbeat(tmp_path):
    out = tmp_path / "logs" / "ocr_status.json"
    code = ocr_io.main(["status", "--out", str(out), "--started", "2026-10-23T04:30:00-0500",
                        "--finished", "2026-10-23T05:30:00-0500", "--head", "abc123",
                        "--exit-code", "3", "--step", "fetch=3:12", "--step", "build=0:300",
                        "--count", "new_ocr_records=186", "--count", "new_summary_records=180"])
    assert code == 0
    status = json.loads(out.read_text())
    assert status["steps"] == [{"name": "fetch", "exit_code": 3, "seconds": 12},
                               {"name": "build", "exit_code": 0, "seconds": 300}]
    assert status["new_ocr_records"] == 186
    assert status["exit_code"] == 3
    assert status["git_head"] == "abc123"
