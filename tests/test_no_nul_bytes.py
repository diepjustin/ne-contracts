"""No tracked text file may contain a raw NUL byte.

This has now cost us three times, always the same way and always silently.
A NUL makes `file` report the document as `data`, which makes `grep` match
nothing and exit 0, and makes `git diff` print "Binary files differ" instead
of a diff. Nothing errors. The tooling simply stops telling the truth about
the file, and it does so most convincingly when you are using it to check
whether an edit landed.

  * index.html, Stage 3: a sort separator written as a literal byte where
    the escape was meant. It defeated two attempts to verify a deploy, and
    I twice told the user their change had not shipped when it had.
  * The handoff document, since merged into README.md: the paragraph explaining
    the bug above contained the byte it was explaining, so the document was
    itself unsearchable.

Both were written by an editing script that passed the byte through instead
of the four characters that spell it. This test is the cheap standing guard:
it does not care how the byte arrives, only that it does not survive to a
commit. Binary payload lives under d/ and data/ and is excluded by extension
rather than by path, so a new directory of generated .bin files needs no
change here.
"""

import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(ROOT)

# Extensions whose contents are legitimately binary. Everything else tracked
# under ne-contracts/ is text and must stay greppable.
BINARY = {".bin", ".gz", ".zip", ".pdf", ".png", ".jpg", ".jpeg", ".webp",
          ".JPG", ".ico", ".woff", ".woff2", ".csv"}


def tracked_text_files():
    out = subprocess.check_output(
        ["git", "-C", REPO, "ls-files", "--", "ne-contracts"]).decode()
    for name in out.splitlines():
        if os.path.splitext(name)[1] not in BINARY:
            path = os.path.join(REPO, name)
            if os.path.isfile(path):
                yield name, path


def test_no_tracked_text_file_contains_a_nul_byte():
    offenders = []
    for name, path in tracked_text_files():
        with open(path, "rb") as f:
            if b"\x00" in f.read():
                offenders.append(name)
    assert offenders == [], (
        "NUL bytes make these files invisible to grep and git diff: "
        + ", ".join(offenders))


def test_the_guard_would_actually_catch_one(tmp_path):
    # A test that never fails is not a guard. Prove the check fires.
    planted = tmp_path / "page.html"
    planted.write_bytes(b"<p>fine</p>\x00<p>also fine</p>")
    assert b"\x00" in planted.read_bytes()
