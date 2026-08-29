"""Re-ask every record we already hold what documents it publishes.

`scrape.py` kept only the first document on a record's detail page until Aug
2026, because `get_view_url` used `soup.find` where it wanted `find_all`. UNL's
Axon contract CW33053 publishes nine and this project held one. A 900-row
sample put the corpus-wide loss at ~67,000 documents, ~93% of them state
agency, with purchase orders sampling 0-for-400.

This fills that in without re-running the scrape. Every row already carries its
Detail URL, so there is no search paging here at all -- which also means the
worst hazard of a long run on this site does not apply: the state's search
results live in server-side state that expires after ~2,000 pages and then
returns "No results found" indistinguishably from the end of the data. Detail
URLs are stateless; 900 of them were fetched cold, with no session or cookie,
while measuring the loss.

  python3 scripts/backfill_documents.py                    # all three, in order
  python3 scripts/backfill_documents.py --dataset contract # one at a time
  python3 scripts/backfill_documents.py --hours 3          # stop cleanly after 3h
  python3 scripts/backfill_documents.py --limit 500        # a slice, for checking
  python3 scripts/backfill_documents.py --status           # progress, no network

**Stopping is normal, not an error.** Ctrl-C once finishes the batch in flight,
writes it and exits; the same command resumes. There is no separate progress
file to drift out of sync with the data -- data/documents.jsonl is itself the
checkpoint, folded on start. That matters: `meta.incomplete`, this project's
most consequential value, has been wrong twice, and the second time was a
checkpoint that had gone missing from the data it described.

There is deliberately no --retry-failed. A row is written only when its page
was actually read, so a row that failed is simply a row not yet recorded, and
the next run picks it up with everything else.
"""

import argparse
import csv
import datetime
import os
import signal
import subprocess
import sys
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

# Imported, never copied. The canary list and the health check have to have
# exactly one definition or they rot apart -- see README.md, "Guard rails".
from scrape import (  # noqa: E402
    DATASETS, DOCUMENTS_JSONL, ROOT, append_documents, document_key,
    documents_entry, document_service_healthy, drain_latency,
    fetch_documents_parallel, load_documents,
)

# Rows per batch. Small enough that a hard kill costs seconds of work, large
# enough to keep all 15 detail workers busy. Each batch is written and fsynced
# before the next starts, so "recorded" always means "on the disk".
BATCH = 50

# How many failures in a row before we stop. A closed lid, a dropped VPN or a
# changed network shows up as every request failing at once, and grinding
# through 400,000 rows to discover that wastes a night. Not a data-safety
# limit -- a failed fetch is never written -- purely a "this is going nowhere".
FAILURE_BREAKER = 25

# How many "the page is fine and offers nothing" answers in a row before we
# stop believing them and re-ask the canaries. This is the 17 Aug 2026 outage
# in the one shape the start-up check cannot catch: the state was serving
# detail pages with the document links simply absent, which is byte-for-byte
# what a record with no document looks like. A run that started healthy and
# went fourteen hours would otherwise write "no document" across the corpus.
EMPTY_BREAKER = 25

# Contracts first: smallest, highest rate of extra documents, and it proves the
# run in ninety minutes. Then state agencies, which hold ~93% of what is
# missing. Purchase orders last -- they sampled 0-for-400 and are being checked
# for certainty rather than expectation.
ORDER = ["contract", "state", "purchase-order"]


class Stopped(Exception):
    """Raised to unwind to the summary. Not an error: the run was asked to stop."""


def install_stop_handler(state):
    """First Ctrl-C asks the run to stop; a second one is the usual hard exit."""
    def handler(_signum, _frame):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        state["stopping"] = True
        print("\n  Stopping after this batch. Press Ctrl-C again to quit now "
              "(the batch in flight is lost, nothing already written is).",
              flush=True)
    signal.signal(signal.SIGINT, handler)


def running_elsewhere():
    """PIDs of other backfill runs on this machine, newest first.

    Asked of `ps` rather than tracked in a pidfile. A pidfile outlives the run
    it describes: the run killed by the laptop's sleep on 28 Aug 2026 would
    have left one behind claiming to be alive, and a run already in flight when
    this was written would have left none at all. `ps` cannot go stale.

    Matching needs care. Naming the script is not enough -- the shell that
    launches `python backfill_documents.py --status` carries the script name in
    its own command line too, and counting it reported "a run is going" beside
    "last wrote 4h ago", which is how this was caught. A run is a *Python
    process* whose arguments include this script, so the interpreter is checked
    as well. Our own process and the shell that started it are excluded outright.

    Returns None if ps cannot be asked -- "we could not find out", which the
    caller reports as such rather than as "nothing is running".
    """
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None

    me = os.path.basename(__file__)
    skip = {os.getpid(), os.getppid()}
    found = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if not pid.isdigit() or int(pid) in skip:
            continue
        argv = command.split()
        if not argv:
            continue
        # argv[0] is the interpreter for a real run and a shell for a wrapper.
        if "python" not in os.path.basename(argv[0]):
            continue
        if any(os.path.basename(arg) == me for arg in argv[1:]):
            found.append(int(pid))
    return found


def last_wrote(path):
    """When the log was last appended to, or None if it does not exist yet.

    The file's own mtime, because that is exactly the question -- no separate
    heartbeat to disagree with the data it claims to describe.
    """
    if not os.path.exists(path):
        return None
    return datetime.datetime.fromtimestamp(os.path.getmtime(path))


def describe_run(path):
    """One line on whether a run is going, for --status."""
    wrote = last_wrote(path)
    when = ""
    if wrote:
        ago = (datetime.datetime.now() - wrote).total_seconds()
        units = (("d", 86400), ("h", 3600), ("m", 60))
        for label, size in units:
            if ago >= size:
                when = f", last wrote {ago / size:.0f}{label} ago"
                break
        else:
            when = f", last wrote {ago:.0f}s ago"

    live = running_elsewhere()
    if live is None:
        return f"could not tell whether a run is going (ps did not answer){when}"
    if live:
        return f"a run is going now (pid {live[0]}){when}"
    if wrote is None:
        return "no run has written anything yet"
    return (f"no run is going{when} — re-run the same command to continue, "
            "or --dataset to pick where")


def rows_for(dataset):
    """Every row of a dataset's CSV, in the order it was written.

    CSV order is scrape order, which is entity by entity. That is deliberate:
    it means a half-finished backfill has *whole agencies* done rather than a
    scattering, which is the same way meta.incomplete already describes partial
    coverage. Ordering by which document types most often carry extras would
    find documents faster and leave coverage far harder to state honestly.
    """
    _entity_type, _doc_type, rel = DATASETS[dataset]
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["Detail URL"]]


def summarise(store, datasets):
    """Per-dataset coverage, folded from the log rather than counted by line.

    `wc -l` on this file overstates by every row ever re-checked; reading an
    append-only log that way once reported 23% complete as 40%.
    """
    print(f"\n{'dataset':<16}{'checked':>12}{'total':>12}{'>1 doc':>9}{'documents':>12}")
    for dataset in datasets:
        rows = rows_for(dataset)
        entries = [store[document_key(r["Detail URL"])] for r in rows
                   if document_key(r["Detail URL"]) in store]
        multi = sum(1 for e in entries if e["n"] > 1)
        docs = sum(e["n"] for e in entries)
        print(f"{dataset:<16}{len(entries):>12,}{len(rows):>12,}{multi:>9,}{docs:>12,}")


def backfill(dataset, store, state, limit=None, deadline=None):
    """Ask every unrecorded row of one dataset what it publishes.

    Returns (recorded, multi_found), and also accumulates both into `state` as
    it goes. The running totals live there rather than only in the return value
    because the expected way out of this function is Stopped -- interrupted, or
    a breaker tripping -- and an exception discards what was returned. Reporting
    "recorded 0" after writing two hundred rows is exactly the wrong thing to
    tell someone deciding whether it is safe to close the laptop.
    """
    rows = rows_for(dataset)
    todo = [r for r in rows if document_key(r["Detail URL"]) not in store]
    already = len(rows) - len(todo)
    if limit is not None:
        todo = todo[:limit]

    # Flushed like the progress lines below: stdout is a pipe when this runs
    # unattended, and an unflushed header means the log looks empty for the
    # first thousand rows -- which reads exactly like a run that never started.
    print(f"\n{dataset}: {len(rows):,} rows, {already:,} already recorded, "
          f"{len(todo):,} to ask" + (f" (limited to {limit:,})" if limit else ""),
          flush=True)
    if not todo:
        return 0, 0

    recorded = multi_found = 0
    failures = empties = 0
    started = time.time()

    for at in range(0, len(todo), BATCH):
        if state["stopping"]:
            raise Stopped("interrupted")
        if deadline and time.time() > deadline:
            raise Stopped("reached the time limit")

        batch = todo[at:at + BATCH]
        results = fetch_documents_parallel([{"detail_url": r["Detail URL"]} for r in batch])

        entries = []
        for row, documents in zip(batch, results):
            if documents is None:
                # Not written. An unrecorded row is indistinguishable from one
                # we have not reached yet, which is exactly the truth, and the
                # next run asks again.
                failures += 1
                if failures >= FAILURE_BREAKER:
                    wrote = append_documents(entries, os.path.join(ROOT, DOCUMENTS_JSONL))
                    recorded += wrote
                    state["recorded"] = state.get("recorded", 0) + wrote
                    raise Stopped(
                        f"{failures} detail pages in a row could not be fetched. "
                        "Nothing was recorded for them. Check the network and "
                        "re-run the same command to pick up where this stopped")
                continue
            failures = 0

            if not documents:
                empties += 1
                if empties >= EMPTY_BREAKER:
                    # Hold this batch back until the canaries answer: if the
                    # service is dark these empties are an outage, not a fact.
                    print(f"    {empties} records in a row offered no document. "
                          "Re-checking the canaries before recording them...",
                          flush=True)
                    if not document_service_healthy():
                        raise Stopped(
                            "the state has stopped serving documents mid-run. "
                            f"The last {empties} records read as though they publish "
                            "nothing, and that is the outage signature, not a fact "
                            "about those records. None of them were recorded")
                    print("    Canaries still serving. These are real absences.",
                          flush=True)
                    empties = 0
            else:
                empties = 0
                if len(documents) > 1:
                    multi_found += 1
                    state["multi"] = state.get("multi", 0) + 1

            entries.append(documents_entry(row["Document Number"], row["Entity Name"],
                                           row["Detail URL"], documents))

        wrote = append_documents(entries, os.path.join(ROOT, DOCUMENTS_JSONL))
        recorded += wrote
        state["recorded"] = state.get("recorded", 0) + wrote

        done = at + len(batch)
        if done % 1000 < BATCH or done >= len(todo):
            elapsed = time.time() - started
            rate = recorded / elapsed if elapsed else 0
            left = (len(todo) - done) / rate if rate else 0
            median = drain_latency()
            print(f"  {done:,}/{len(todo):,} ({100 * done / len(todo):.1f}%) — "
                  f"{multi_found:,} with several documents — {rate:.1f} rows/s"
                  + (f", median {median:.2f}s" if median else "")
                  + f", ~{left / 3600:.1f}h left", flush=True)

    return recorded, multi_found


def main():
    parser = argparse.ArgumentParser(
        description="Capture every document each already-scraped record publishes.")
    parser.add_argument("--dataset", choices=list(DATASETS),
                        help="just this one; default is all three, contracts first")
    parser.add_argument("--limit", type=int,
                        help="stop after this many rows per dataset (for checking a slice)")
    parser.add_argument("--hours", type=float, help="stop cleanly after this long")
    parser.add_argument("--status", action="store_true",
                        help="report coverage and exit; makes no network calls")
    args = parser.parse_args()

    datasets = [args.dataset] if args.dataset else ORDER
    store = load_documents(os.path.join(ROOT, DOCUMENTS_JSONL))

    if args.status:
        print(describe_run(os.path.join(ROOT, DOCUMENTS_JSONL)))
        summarise(store, datasets)
        return

    # Its own refusal, not one inherited from somewhere upstream. A run started
    # while the state is dark would record "no document" for everything it
    # touched, and those are the writes that are hardest to notice and undo.
    if not document_service_healthy():
        sys.exit("ERROR: the state is not serving documents right now, so every "
                 "record would read as though it publishes none. Refusing to run. "
                 "Try again once https://statecontracts.nebraska.gov is serving.")

    state = {"stopping": False, "recorded": 0, "multi": 0}
    install_stop_handler(state)
    deadline = time.time() + args.hours * 3600 if args.hours else None

    stopped_because = None
    for dataset in datasets:
        try:
            backfill(dataset, store, state, args.limit, deadline)
        except Stopped as why:
            stopped_because = str(why)
            break

    print(f"\nRecorded {state['recorded']:,} record(s) this run; {state['multi']:,} "
          "publish more than one document.")
    if stopped_because:
        print(f"Stopped: {stopped_because}.")
        print("Re-run the same command to continue where this left off.")
    summarise(load_documents(os.path.join(ROOT, DOCUMENTS_JSONL)), datasets)


if __name__ == "__main__":
    main()
