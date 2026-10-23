"""Check a JSONL file is fit to publish as a release asset, and loses nothing.

Used by scripts/publish_release_asset.sh before it uploads data/ai_summaries.jsonl
or data/scope.jsonl. Stricter than the build's own readers on purpose: those
tolerate a torn last line because an asset can be cut from a file mid-write,
and this is the step that makes sure a published one never is.

    ai-summaries  every line parses; every record that carries a summary has a
                  token and a model listed in build_site.AI_MODELS (the build
                  refuses anything else, so a bad model would fail every build
                  until the asset was replaced)
    scope         every line parses, with no blank lines (build_site reads this
                  file with a bare json.loads per line); every record has a
                  token and a non-empty description

Both must end with a newline. Given --published, the new file must also keep
every token the published one has, every token that has text there still has
text here, and the record count must not fall: the Mac's file only grows, so
anything else means the wrong file (a stale copy, a partial one) is about to
replace a better one.

    python3 scripts/check_release_asset.py ai-summaries data/ai_summaries.jsonl \\
        --published /tmp/old.jsonl --env

Exit 0 when fit to publish, 1 with the reasons on stderr otherwise. --env prints
RECORDS=, WITH_TEXT=, TOKENS= and SHA256= lines for the shell to read.
"""

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

KINDS = ("ai-summaries", "scope")
MAX_EXAMPLES = 5


class Scan:
    def __init__(self):
        self.records = 0
        self.tokens = set()
        self.with_text = set()
        self.sha256 = None
        self.problems = []


def known_models():
    # Read from the build, never copied: the build is what refuses a model.
    from build_site import AI_MODELS
    return {m for m in AI_MODELS if m}


def scan(kind, path):
    result = Scan()
    digest = hashlib.sha256()
    models = known_models() if kind == "ai-summaries" else None
    bad = []

    with open(path, "rb") as f:
        last = b""
        for number, blob in enumerate(f, 1):
            digest.update(blob)
            last = blob
            if not blob.strip():
                if kind == "scope":
                    bad.append(f"line {number} is blank (build_site.py cannot read a blank line here)")
                continue
            try:
                record = json.loads(blob.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as e:
                bad.append(f"line {number} is not valid JSON ({e})")
                continue
            if not isinstance(record, dict):
                bad.append(f"line {number} is not a JSON object")
                continue
            result.records += 1
            tok = record.get("tok")

            if kind == "ai-summaries":
                has_text = bool(record.get("summary")) and not record.get("unclear")
                if has_text and not tok:
                    bad.append(f"line {number} has a summary but no token")
                if has_text and record.get("model") not in models:
                    bad.append(f"line {number} has a summary from model {record.get('model')!r}, "
                               f"which build_site.AI_MODELS does not list")
                if not tok and not ("error" in record or "crash" in record):
                    bad.append(f"line {number} has no token")
            else:
                has_text = isinstance(record.get("description"), str) and bool(record["description"])
                if not tok:
                    bad.append(f"line {number} has no token")
                elif not has_text:
                    bad.append(f"line {number} has no description")

            if tok:
                result.tokens.add(tok)
                if has_text:
                    result.with_text.add(tok)

        if last and not last.endswith(b"\n"):
            bad.append("the file does not end with a newline: it was copied mid-write")

    result.sha256 = digest.hexdigest()
    if bad:
        result.problems.append(f"{len(bad):,} bad line(s) in {path}: " + "; ".join(bad[:MAX_EXAMPLES])
                               + (" ..." if len(bad) > MAX_EXAMPLES else ""))
    return result


def compare(new, old):
    """Reasons the new file must not replace the published one; empty when it may."""
    problems = []
    if new.records < old.records:
        problems.append(f"it has fewer records than the published copy "
                        f"({new.records:,} against {old.records:,})")
    lost = old.tokens - new.tokens
    if lost:
        problems.append(f"{len(lost):,} token(s) in the published copy are missing from it, e.g. "
                        + ", ".join(sorted(lost)[:MAX_EXAMPLES]))
    lost_text = (old.with_text - new.with_text) - lost
    if lost_text:
        problems.append(f"{len(lost_text):,} token(s) have text in the published copy and none in it, "
                        "e.g. " + ", ".join(sorted(lost_text)[:MAX_EXAMPLES]))
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("path")
    parser.add_argument("--published", help="the currently published copy, uncompressed")
    parser.add_argument("--env", action="store_true",
                        help="print RECORDS=, WITH_TEXT=, TOKENS= and SHA256= for the shell")
    args = parser.parse_args(argv)

    new = scan(args.kind, args.path)
    problems = list(new.problems)
    if not new.records:
        problems.append(f"{args.path} holds no records")

    if args.published:
        old = scan(args.kind, args.published)
        # A published copy with its own problems is still a floor: what it does
        # hold must not be lost. Its problems are reported, not fatal.
        for p in old.problems:
            print(f"note: the published copy has problems of its own: {p}", file=sys.stderr)
        problems += [f"refusing to replace the published copy: {p}" for p in compare(new, old)]
        print(f"published copy: {old.records:,} records, {len(old.with_text):,} with text",
              file=sys.stderr)

    print(f"{args.path}: {new.records:,} records, {len(new.tokens):,} tokens, "
          f"{len(new.with_text):,} with text, sha256 {new.sha256}", file=sys.stderr)
    if problems:
        for p in problems:
            print(f"ERROR: {p}", file=sys.stderr)
        return 1
    if args.env:
        print(f"RECORDS={new.records}")
        print(f"WITH_TEXT={len(new.with_text)}")
        print(f"TOKENS={len(new.tokens)}")
        print(f"SHA256={new.sha256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
