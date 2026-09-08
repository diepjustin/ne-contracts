#!/usr/bin/env python3
"""Lint the inline script in index.html.

The page is one self-contained file by design, so there is no .js for eslint to
read. This cuts the script out into .lint/, keeping every line at the line
number it has in index.html so a report points at the real file, and runs
eslint over it through npx -- no package.json, no lockfile, no node_modules in
the repo.

    python3 scripts/lint_page.py            # lint, exit non-zero on any problem

Why it exists: a local `var end` in render() shadowed the module-level `end`
column and blanked every End cell in the table from the first build onwards.
The payload was correct, so the crc32 selftest passed the entire time. Nothing
in this repo could have found it except a linter.
"""

import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "index.html")
CONFIG = os.path.join(ROOT, "eslint.config.mjs")
OUTDIR = os.path.join(ROOT, ".lint")

# Pinned rather than floating. An eslint that changes under the workflow turns
# a green build red on someone else's schedule, and this runs on every push.
ESLINT = "eslint@10.10.0"

SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def extract(html):
    """Inline script bodies as (start_line, text), 1-based, in document order."""
    out = []
    for m in SCRIPT_RE.finditer(html):
        body = m.group(1)
        # The line the body starts on: everything before it, counted. A script
        # whose content begins on the same line as the tag keeps that line.
        start = html.count("\n", 0, m.start(1)) + 1
        out.append((start, body))
    return out


def as_padded_js(html):
    """One .js whose line N is line N of the page.

    Blank-padding rather than a source map: eslint reports a line number and
    that number has to be the one you can open index.html at, or the report is
    another thing to translate by hand at the moment you least want to.
    """
    blocks = extract(html)
    if not blocks:
        raise SystemExit("index.html: no inline <script> found")
    lines = [""] * html.count("\n")
    for start, body in blocks:
        for offset, line in enumerate(body.split("\n")):
            at = start - 1 + offset
            if at < len(lines):
                lines[at] = line
    return "\n".join(lines) + "\n"


def main():
    if shutil.which("npx") is None:
        print("lint_page: npx not found -- install Node to lint the page", file=sys.stderr)
        return 2

    with open(PAGE, encoding="utf-8") as fh:
        html = fh.read()

    os.makedirs(OUTDIR, exist_ok=True)
    target = os.path.join(OUTDIR, "index.html.js")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(as_padded_js(html))

    proc = subprocess.run(
        ["npx", "--yes", ESLINT, "--no-config-lookup", "-c", CONFIG,
         "--format", "json", target],
        cwd=ROOT, capture_output=True, text=True,
    )
    try:
        results = json.loads(proc.stdout)
    except ValueError:
        # eslint failed before it could report: a bad config, no network for
        # npx. Say so with whatever it printed rather than passing silently.
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        print("lint_page: eslint produced no report", file=sys.stderr)
        return 2

    problems = 0
    for result in results:
        for m in result.get("messages", []):
            problems += 1
            rule = m.get("ruleId") or "syntax"
            print("index.html:%s:%s  %s  %s"
                  % (m.get("line", 0), m.get("column", 0), m["message"], rule))
    if problems:
        print("\n%d problem(s)" % problems)
        return 1
    print("index.html: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
