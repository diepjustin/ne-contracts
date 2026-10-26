"""Shared plumbing for the Mac's nightly OCR job (scripts/run_ocr_catchup.sh).

Standard library only, so it runs in whatever venv the Mac's job has.

Four things, each small, each easy to get subtly wrong once per script:

  iter_jsonl     reads an append-only log another process may be writing. A last
                 line with no newline is a write still in progress (or one a kill
                 cut short), so it is skipped, never parsed and never truncated:
                 the file belongs to whoever writes it. A bad line with more lines
                 after it still raises, because what follows it cannot be trusted.
  append_jsonl   appends records, starting with a newline when the file does not
                 end with one, so a record can never be glued onto a torn tail.
  the run lock   an exclusive flock on data/.ocr.lock, so two runs of the job
                 (or a hand-run publish during the nightly) cannot interleave.
  exit codes     a run where more than half of this run's items failed exits 2.

It is also a small command line for the wrapper, which is bash and should not
grow JSON handling of its own:

    python3 scripts/ocr_io.py lock --fd 9      # flock an fd the shell opened
    python3 scripts/ocr_io.py prune-builds     # delete d/* but manifest.json's
    python3 scripts/ocr_io.py status --out logs/ocr_status.json ...
"""

import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(ROOT, "data", ".ocr.lock")

# The wrapper opens the lock file on this fd number, locks it, and tells its
# children which fd that is. A child re-locks the same open file description,
# which succeeds -- and proves the lock really is held, rather than trusting a
# variable that could have leaked in from anywhere.
LOCK_FD_ENV = "NE_OCR_LOCK_FD"

# EX_TEMPFAIL: "try again later" is exactly what a busy lock means.
LOCK_BUSY_EXIT = 75
# The Python steps' "this run mostly failed" code. 1 stays what it already
# meant: the step refused to start (no Ollama, state not serving documents).
FAILED_RUN_EXIT = 2

BUILD_DIR_NAME = re.compile(r"^\d{8}-\d{6}$")


class LockBusy(Exception):
    pass


# --- JSONL ---------------------------------------------------------------------------------

def iter_jsonl(path, on_torn=None):
    """Yield (line number, record) for each complete line of an append-only log.

    Streams the file in binary, split on b"\\n" alone (json.dumps with
    ensure_ascii=False leaves U+2028 and friends raw inside strings, and text-mode
    splitting would cut those records in two). Blank lines are skipped. The final
    line, if it has no newline, is treated as torn: on_torn(number, blob) is
    called and it is not parsed -- even when it happens to be valid JSON, since a
    writer that stopped there may not have finished.
    """
    with open(path, "rb") as f:
        for number, blob in enumerate(f, 1):
            if not blob.endswith(b"\n"):
                if blob.strip() and on_torn is not None:
                    on_torn(number, blob)
                return
            if not blob.strip():
                continue
            try:
                record = json.loads(blob.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as e:
                raise ValueError(f"{path} line {number} is not valid JSON ({e}), and more "
                                 "lines follow it; the entries after it cannot be trusted.") from e
            yield number, record


def ends_with_newline(path):
    """True for a missing or empty file too: neither needs a separator."""
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return True
    if size == 0:
        return True
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"


def append_jsonl(path, records):
    """Append one JSON line per record, in a single write.

    A file whose last byte is not a newline gets one first. The loaders cut a torn
    tail before a run appends anything, but a tail that is complete JSON missing
    only its newline survives that, and the next record would otherwise be glued
    onto it -- one broken line in the middle of the file, which stops every reader.
    """
    if not records:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    lead = "" if ends_with_newline(path) else "\n"
    chunk = lead + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    with open(path, "a", encoding="utf-8") as f:
        f.write(chunk)
        f.flush()


@contextlib.contextmanager
def atomic_write(path, mode="w", encoding="utf-8"):
    """Write to a temporary file beside `path`, then os.replace it into place.

    Beside it, not in /tmp: os.replace cannot cross filesystems. A reader of
    `path` sees either the old file or the whole new one, never a half-written one.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        kwargs = {} if "b" in mode else {"encoding": encoding}
        with os.fdopen(fd, mode, **kwargs) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


# --- the run lock --------------------------------------------------------------------------

def _same_file(fd, path):
    try:
        a, b = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def lock_fd(fd):
    """Take the exclusive lock on an already-open fd, without waiting."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            raise LockBusy() from e
        raise


@contextlib.contextmanager
def run_lock(path=None):
    """Hold the OCR job's lock for the duration of the block, or raise LockBusy.

    Under the wrapper the lock is already held on an inherited fd (LOCK_FD_ENV);
    re-locking that same open file description succeeds, and it is never unlocked
    here, since that would release the wrapper's lock too. Anywhere else this
    opens the lock file itself and releases it on exit.
    """
    path = path or LOCK_PATH
    inherited = os.environ.get(LOCK_FD_ENV, "")
    if inherited.isdigit() and _same_file(int(inherited), path):
        lock_fd(int(inherited))
        yield
        return

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        lock_fd(fd)
        yield
    finally:
        os.close(fd)  # closing the only description releases the lock


def lock_or_exit(path=None):
    """run_lock() for a script's main(): a busy lock is a clear message and exit 75."""
    @contextlib.contextmanager
    def held():
        try:
            with run_lock(path):
                yield
        except LockBusy:
            print(f"ERROR: another OCR or summary run holds {path or LOCK_PATH}; "
                  "nothing was read or written. Try again once it finishes.", file=sys.stderr)
            sys.exit(LOCK_BUSY_EXIT)
    return held()


# --- exit codes ----------------------------------------------------------------------------

def failed_run_exit(errors, total):
    """FAILED_RUN_EXIT when more than half of this run's items errored, else 0.

    A run with nothing to do is a success. "More than half" rather than "any":
    a handful of documents the state will not serve is a normal night.
    """
    if total and errors * 2 > total:
        return FAILED_RUN_EXIT
    return 0


# --- the wrapper's commands ----------------------------------------------------------------

def prune_builds(root=ROOT):
    """Delete every d/<build id> except the one manifest.json points at.

    Returns the names removed. Refuses (ValueError) when manifest.json is missing,
    unreadable or names a directory that is not there: deleting "everything else"
    is only safe when there is a known-good build left standing. Only names shaped
    like a build id (YYYYMMDD-HHMMSS) are touched.
    """
    manifest = os.path.join(root, "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as f:
            keep = json.load(f)["dir"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"cannot read the current build from {manifest}: {e}") from e
    head, name = os.path.split(os.path.normpath(keep))
    if head != "d" or not BUILD_DIR_NAME.match(name):
        raise ValueError(f"manifest.json names {keep!r}, not a d/<build id> directory")
    builds = os.path.join(root, "d")
    if not os.path.isdir(os.path.join(builds, name)):
        raise ValueError(f"manifest.json names {keep}, which does not exist")
    removed = []
    for entry in sorted(os.listdir(builds)):
        path = os.path.join(builds, entry)
        if entry != name and BUILD_DIR_NAME.match(entry) and os.path.isdir(path) \
                and not os.path.islink(path):
            shutil.rmtree(path)
            removed.append(entry)
    return removed


def write_status(path, status):
    with atomic_write(path) as f:
        json.dump(status, f, indent=2, sort_keys=True)
        f.write("\n")


def parse_step(spec):
    """name=code:seconds, as the wrapper records each step."""
    name, _, rest = spec.partition("=")
    code, _, seconds = rest.partition(":")
    if not name or not code.lstrip("-").isdigit() or not seconds.isdigit():
        raise argparse.ArgumentTypeError(f"expected name=code:seconds, got {spec!r}")
    return {"name": name, "exit_code": int(code), "seconds": int(seconds)}


def parse_count(spec):
    name, _, value = spec.partition("=")
    if not name or not value.lstrip("-").isdigit():
        raise argparse.ArgumentTypeError(f"expected name=integer, got {spec!r}")
    return name, int(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("lock", help="flock an fd this process inherited, without waiting")
    p.add_argument("--fd", type=int, required=True)

    p = sub.add_parser("prune-builds", help="delete d/* except the build manifest.json names")
    p.add_argument("--root", default=ROOT)

    p = sub.add_parser("status", help="write the job's heartbeat file atomically")
    p.add_argument("--out", required=True)
    p.add_argument("--started", required=True)
    p.add_argument("--finished", default=None)
    p.add_argument("--head", default=None)
    p.add_argument("--source", default=None, help="where step 1 got the data from")
    p.add_argument("--exit-code", type=int, default=None)
    p.add_argument("--step", type=parse_step, action="append", default=[])
    p.add_argument("--count", type=parse_count, action="append", default=[])

    args = parser.parse_args(argv)

    if args.command == "lock":
        try:
            lock_fd(args.fd)
        except LockBusy:
            print("another OCR run holds the lock", file=sys.stderr)
            return LOCK_BUSY_EXIT
        return 0

    if args.command == "prune-builds":
        try:
            removed = prune_builds(args.root)
        except ValueError as e:
            print(f"not pruning: {e}", file=sys.stderr)
            return 1
        print(f"pruned {len(removed)} old build(s)" + (": " + ", ".join(removed) if removed else ""))
        return 0

    status = {
        "started": args.started,
        "finished": args.finished,
        "git_head": args.head,
        "source": args.source,
        "exit_code": args.exit_code,
        "steps": args.step,
    }
    status.update(dict(args.count))
    write_status(args.out, status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
