"""Merge a released document log into the cached nightly one, the newest entry per record winning.

    python3 scripts/merge_document_logs.py RELEASE CACHED OUT

pages.yml builds from data/documents.jsonl, the log --daily appends to in the Actions
cache, and merges in documents.jsonl.gz from the extraction-data release: the 28-30 Aug
backfill, or a later sitting re-uploaded from the Mac. scrape.load_documents folds a log
last-entry-wins, so the merge's whole job is to leave each record's newest entry last.

Until Nov 2026 pages.yml did that by file order: `cat cached release`, so the release won
every record both held, as "the authoritative backfill". It stopped being authoritative
once --daily began re-reading the document lists of amended, restored and swept records.
On 5 Nov the cached log held every record the release did, and was newer on 207 of them:
release-last put the August list back on those, hiding 235 documents the state had added
since, usually the amendment itself. Swapping the order would only have moved the problem:
a backfill re-uploaded after a new sitting would then lose every record to older nightly
reads. So the winner is decided by each entry's `seen` date, not by which file it is in:

- every line of CACHED is written, in order, so the cached log's own last-entry-wins holds;
- a RELEASE line is written after them only when its record is not in CACHED, or its `seen`
  is later than every CACHED entry for that record. A tie goes to the cached entry. An
  entry without `seen` counts as older than any that has one.

A torn last line -- what a write killed mid-line leaves -- is dropped from either file with
a note, exactly as load_documents would truncate it, so it can never end up glued to the
next file in the middle of OUT, where load_documents refuses to read past it and every
build would fail. A broken line anywhere else stops the merge, for load_documents' reason:
something rewrote that log rather than appending to it.

Standard library only. OUT is written whole and then renamed, so a killed merge leaves no
half-written log behind.
"""
import json
import os
import sys


def entries(path):
    """(raw line ending in a newline, parsed entry) for every line of a JSONL log.

    Reads one line ahead so the last line is known to be the last, without holding the
    whole file: the cached log is ~120 MB.
    """
    with open(path, "rb") as f:
        lines = iter(f)
        line = next(lines, None)
        number = 0
        while line is not None:
            following = next(lines, None)
            number += 1
            if line.strip():
                try:
                    entry = json.loads(line)
                except ValueError:
                    if following is None:
                        print(f"{path} ends mid-write; dropping its last line ({len(line)} bytes)")
                        return
                    raise SystemExit(f"{path} line {number} is not valid JSON, and it is not "
                                     "the last line; refusing to merge a log something rewrote")
                yield (line if line.endswith(b"\n") else line + b"\n"), entry
            line = following


def merge(release, cached, out):
    """Write the merged log to `out`; return a dict of counts for the log line."""
    newest = {}
    counts = {"cached": 0, "release": 0, "release_newer": 0, "release_only": 0}
    tmp = out + ".partial"
    with open(tmp, "wb") as f:
        for raw, entry in entries(cached):
            f.write(raw)
            counts["cached"] += 1
            seen = entry.get("seen") or ""
            if seen > newest.get(entry["k"], ""):
                newest[entry["k"]] = seen
            else:
                newest.setdefault(entry["k"], seen)
        for raw, entry in entries(release):
            counts["release"] += 1
            key = entry["k"]
            if key not in newest:
                counts["release_only"] += 1
            elif (entry.get("seen") or "") > newest[key]:
                counts["release_newer"] += 1
            else:
                continue
            f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out)
    return counts


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        raise SystemExit(__doc__.split("\n\n")[1].strip())
    release, cached, out = args
    c = merge(release, cached, out)
    kept = c["release_only"] + c["release_newer"]
    print(f"document logs: {c['cached']:,} cached entries kept; of {c['release']:,} released, "
          f"{kept:,} added ({c['release_only']:,} for records the cached log lacks, "
          f"{c['release_newer']:,} newer than its entry), {c['release'] - kept:,} older or the same")
    return 0


if __name__ == "__main__":
    sys.exit(main())
