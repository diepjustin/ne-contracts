# Nebraska State Contracts

**Live: https://diepjustin.github.io/ne-contracts/**

A journalism and accountability tool for scraping and publishing Nebraska state spending
records from the [Nebraska State Contracts Database](https://statecontracts.nebraska.gov/Search),
the public database mandated by **Neb. Rev. Stat. § 84-602.04**.

Two things live here: a scraper, and a static searchable site built from what it
collects. `index.html` sits at the folder root because that root is the published URL.
There is no backend and no framework.

**739,605 records · 540,074 descriptions · 92 entities · one 6.81 MB page.**

| | |
|---|---|
| [What's in the data](#whats-in-the-data) | coverage, caveats, descriptions |
| [Running it](#running-it) | scrape, build, preview, restore |
| [How it works](#how-it-works) | scraper, payload, publishing |
| [Guard rails](#guard-rails) | read before changing anything that writes |
| [Things that bit us](#things-that-bit-us) | expensive to rediscover |
| [Open work](#open-work) | what is unfinished, and what is not worth doing |

---

## What's in the data

**739,605 records across 92 entities** — every state agency, board and commission the
database lists (83), plus all nine University of Nebraska and Nebraska State College
campuses. Both Active and Expired, contracts and purchase orders. **Collection is
complete.**

| | Records |
| --- | ---: |
| University of Nebraska Medical Center | 238,763 |
| 70 state agencies with records (13 have none) | 235,169 |
| University of Nebraska Lincoln | 209,526 |
| University of Nebraska Omaha | 28,167 |
| University of Nebraska Kearney | 18,696 |
| Wayne State College | 5,562 |
| Chadron State College | 871 |
| University of Nebraska Central Administration | 821 |
| Peru State College | 489 |
| Nebraska State College System | 131 |

Two campuses hold 61% of everything, almost all purchase orders. On the agency side
three dominate: Correctional Services (61,783), Health & Human Services (60,682) and
Roads (39,253) are about 70% of state agency records.

The page names any outstanding entity itself, reading the scrapers' checkpoints, so a
partly-collected entity is never silently indistinguishable from an empty one.

### Caveats worth knowing before you quote it

The scraper reproduces the state's records faithfully, including their errors.

- **This is not a complete record of state spending, and the gaps are not random.** The
  state's own [FAQ](https://das.nebraska.gov/materiel/contract-database/faq.html) says
  the database excludes contracts from Health & Human Services, the University, the
  State Colleges, Veterans' Affairs, Education, the Commission for the Blind and
  Visually Impaired, and the Nebraska Investment Finance Authority "that provide
  specific aid, assistance, or services to a specific individual." Those are among the
  largest agencies here, so their totals understate what they actually spend. **Do not
  read an agency's total as its budget.**
- **A handful of records carry billion-dollar values and will dominate any total.** The
  largest is `95601`, HHS / "CREIGHTON UNIVERSITY - ALL PAYMENTS", at $38,025,000,000,
  then three Medicaid managed-care contracts at $6,650,000,000 each and `58-1-1451`
  (EBSCO, UNL) at $4,000,000,000. Some are plausibly real not-to-exceed ceilings on
  multi-year statewide programs rather than errors — the state's FAQ says service
  contracts are valued at the estimated cost of the whole contract including renewals.
  Treat the top of the amount column as ceilings, not money spent.
- **Vendor names are fragmented, and the site only partly repairs it.** The state
  records one firm many ways: 26 spellings of Hausmann Construction total $1.40 B where
  the largest single spelling shows $903 M. About 9,000 vendor strings sit in a cluster
  with at least one other, holding **$11.02 B** between them. Four companies have been
  reviewed by hand and grouped; the rest have not. See [Open work](#open-work).
- **Purchase orders are not a Higher Education thing, even though the site makes them
  look like one.** The search form only offers a Contract/Purchase Order filter for
  Higher Education, so on the agency side the distinction is invisible — but it is in
  the records. Ten of the 32 agency document-type codes (`O9`, `OM`, `OP`, `X7`, `Y6`,
  `Y7`, `Z8`, `Z9`, `ZO`, `ZP`) are filed under "Purchase Orders", covering 152,210
  records, about 65% of agency data. Overall the dataset is 619,601 purchase orders to
  118,594 contracts — the reverse of what the interface implies.
- **Document types are the source system's internal codes**, not labels. The state
  publishes no key. `scripts/type_groups.json` maps each to the category its detail page
  files it under, which is what the page filters on; the raw code stays in the table
  (hover the Type cell) and in the CSV export. That mapping is a sampled observation,
  and one code contradicts it: `PO`, which the site's own Purchase Order search returns,
  has a detail page headed "Contracts". The map overrides it, since the search filter is
  the better authority.
- **Only documents active on or after 1 January 2014 are in the database at all.**
  Anything that expired before then was never loaded, so early years are sparse in a way
  that reflects the database's construction rather than state spending.
- **The same document number can appear twice under one agency.** Usually a vendor
  rename, sometimes a genuine amendment: `45500` at the Medical Center is $2,558,983
  expired and $21,204,743 active. 176 triples repeat; 5 are two different documents.
- **Document `41780` begins 09/28/2223** and ends 09/28/2023. Filter on begin > end to
  find that class of error — the page has a checkbox for it.
- Open-ended records commonly carry an end date of `12/31/2099` or `01/01/2099`.
- Amounts are as recorded by the state and may not reflect amendments. `--daily` does not
  rewrite an amended row — the CSV keeps the value from the last full scrape — but it does
  record the movement to `data/changes.jsonl`. Only a full re-scrape updates the row
  itself.
- The state's own entity list contains a typo — "Deaf & Hard of Dearing" — preserved
  verbatim, because matching the source exactly is what makes the links work.
- **The state updates daily** (per its FAQ).

### Descriptions

The state publishes no "scope of work" field, so there is nothing to scrape for it. But
most documents contain one, written by whoever filed them, and `scripts/extract_scope.py`
lifts it out of the PDF **verbatim**. Nothing is generated, summarised or rewritten —
every description on the site is the state's own words, which is what makes it quotable.
Typos, inconsistent capitalisation and abbreviations are theirs and are kept.

**540,074 of 739,605 rows have one**, 94.8% of every document that could be read at all.
They come from three places, in this order of preference:

| source | rows | what it is |
|---|---:|---|
| line items | 533,497 | what was bought, off the purchase-order table |
| cover sheet | 3,836 | a summary someone wrote by hand on a University contract |
| SERVICES clause | 2,737 | the contract stating its own scope, where there is no cover sheet |

A fourth source, `cover_sheet_form`, is in `extract_scope.py` but **not yet in the
published data** — it lands at the next extraction run. The University has a second
cover sheet that is a filled PDF form rather than prose, and DocuSign flattens it on
export: every field label is written out in one run, then every filled value in another,
so "DESCRIPTION OF PURCHASE" ends up dozens of lines from its own answer. It recovers
**548 descriptions** out of the 22,086 readable documents nothing else describes — 2.5%,
which is worth having and is nowhere near a solution.

**How a positional parser was checked.** Nothing in that form identifies the description
except its position in the value run, which is how one contract's words get attributed to
another. `scripts/verify_form_geometry.py` answers an independent question: it downloads
the real PDF and reads where the ink is, taking whatever sits level with and to the right
of the "DESCRIPTION OF PURCHASE" label, then compares that against what the parser
claimed from flat text. The first sample of 40 agreed 37 times; the three failures were
real and none of them would have been caught by a test:

- two documents whose description field was simply blank, so the term dates slid into its
  place and would have been published as the contract's scope
- one that extracted as `/\x04EZ\x03\x11h^` where the page plainly reads "travel" — a font
  encoding pypdf cannot map

With guards for both, a fresh sample of 60 agreed 60 times. This is the same lesson as
the truncation bug, in a new form: the tests all passed both times, and only the source
document settled it. Geometry cannot be used in the parser itself — doc_text.jsonl holds
no coordinates and re-reading 739,605 PDFs is a 20+ hour download — so it stays a
sampling check.

Things worth knowing before quoting one:

- **A description is one document's text, not the contract's official scope.** It is
  whatever the filer typed. Read the source document before quoting; every row links to
  it.
- **Descriptions carry administrative text alongside the substance** — invoicing
  instructions, project numbers, contact names, change-order logs. These are kept
  deliberately rather than filtered, because on some documents the actual scope appears
  *after* the boilerplate, so a filter aimed at the noise removes the substance.
- **A blank is not a statement about the contract.** It means we could not read a
  description out of the document, not that the contract has no scope. The page says so
  where it happens rather than leaving an empty cell to be misread. It deliberately does
  not say *why*, because the reason varies and we were getting it wrong. Measured over
  the 118,843 blanks where the state does publish a document:

  | why it is blank | documents | share |
  |---|---:|---:|
  | the PDF is a scan with no text layer | 89,180 | 75.0% |
  | readable text, but our parsers found no description in it | 22,086 | 18.6% |
  | the state says the item was a direct purchase with no contract | 7,577 | 6.4% |

  That last group is not a failure of any kind: those documents consist of a single
  sentence from the state — *"This item involved a direct purchase which did not result
  in a contract. Therefore, there is no contract available for this item."* — and are
  almost entirely Department of Roads spending.
- **Searching descriptions is opt-in.** The index is a separate 9.92 MB download, fetched
  only when you tick the box, so it costs nothing for readers who do not use it.
- **Filtering by whether a row has one is not.** The page's Description control —
  all records / has one / has none — reads `descsrc.bin`, which is resident, and each
  description is captioned with where it came from. Both exist because the same text
  reads completely differently depending on which parser produced it, and because a
  reader clicking University contracts hits a blank five times in six and reasonably
  concludes the database has no descriptions at all.

**94.8% is a coverage figure for readable documents, and it hides where the gap is.**
Purchase orders carry the vast majority of the descriptions, and a purchase order's
description is a list of what was bought. Contracts — the records worth reading — are
where there is almost nothing:

| | rows | has a description | blank, document exists | no document |
| --- | ---: | ---: | ---: | ---: |
| University contracts | 35,681 | **12.8%** | 82.9% | 4.3% |
| State agency records | 235,410 | **42.0%** | 40.3% | 17.7% |
| Purchase orders | 468,642 | **93.2%** | 5.9% | 0.8% |

**On a large share of University contracts the scope is not in the document at all.**
1,161 of the blank ones carry the identical sentence *"...to render the services and
provide the deliverables identified in Section 1 of Exhibit A"*, and the state publishes
the agreement without the exhibit. That is a limit of what is disclosed, not of the
parsers: no pattern and no OCR run reaches a scope that was never filed. A heading-based
parser was measured against those 7,165 documents and rejected — 56% of what it returned
was template boilerplate repeated across hundreds of contracts and 32% began mid-sentence,
which would have published a tax clause as a contract's scope. Guarded hard enough to be
safe it reached 154 documents, 2.1%, and still got Oracle's warranty clause. This agrees
with the 5.4% ceiling under "Open work"; do not spend a week here either.

---

## Running it

```bash
cd ne-contracts
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt

python3 scripts/scrape.py contract          # -> data/nu_contracts.csv
python3 scripts/scrape.py purchase-order    # -> data/nu_purchase_orders.csv
python3 scripts/scrape.py state             # -> data/state_agencies.csv
python3 scripts/scrape.py state --status    # progress, no network
python3 scripts/scrape.py state --hours 3   # stop cleanly after 3h
python3 scripts/scrape.py state --entity "Roads, Department of"
python3 scripts/check_entity_drift.py       # has the state's entity list changed?

python3 scripts/extract_text.py             # -> data/doc_text.jsonl (the 36h one)
python3 scripts/extract_text.py --status    # progress, no network
python3 scripts/extract_text.py --retry-errors    # re-ask the 14,586 that failed
python3 scripts/extract_text.py --retry-non-pdf   # re-ask the 32,227 non-PDF answers
python3 scripts/extract_scope.py            # doc_text.jsonl -> data/scope.jsonl (~1 min)

python3 scripts/build_site.py               # data/*.csv -> d/<buildId>/  (~3 min, ~1.7 GB peak)
python3 scripts/serve_site.py               # preview at http://127.0.0.1:8765
python3 -m pytest tests/ -q
```

`extract_text.py` refuses to start while the state is not serving documents, for the
reason under "Things that bit us". `--status` still answers, because it makes no network
calls.

Every scrape checkpoints per page and resumes; interrupting is safe. A full run is 20+
hours against a government server — **don't re-scrape casually.**

Output columns: `Document Number`, `Document Type`, `Entity Code`, `Entity Name`,
`Vendor`, `Amount`, `Begin Date`, `End Date`, `Status`, `Detail URL`, `View URL`.
`Entity Name` holds the canonical dropdown name rather than the site's rendering of it
(state result grids shout it: `DRY BEAN COMMISSION`), because both the resume checkpoint
and `build_site.py`'s entity guard match on that exact string.

### Daily updates

The full scrape is a one-time job. Day to day the state adds records under
`Status=Active`, only ~7.3% of pages, so `--daily` re-scans just that status and diffs it
against what the CSV already knows, matched on `Detail URL`:

```bash
python3 scripts/scrape.py contract --daily
python3 scripts/scrape.py purchase-order --daily
python3 scripts/scrape.py state --daily
```

An unseen record gets a detail fetch and a new row. A known record is not rewritten. A
previously-Active record that does not appear today has its `Status` flipped to `Expired`
in place — **unless the disappearance is too large to be real**. An entity that returns
no records at all while it had active ones, or that loses more than half of 20 or more,
is refused and re-checked on the next run. That is not hypothetical tidiness: without it,
one outage expired all 44,063 active records in a single green run (see "Things that bit
us").

**Amendments are recorded rather than discarded.** Amount, vendor, begin and end date are
compared against what the CSV already held, and any move is appended to
`data/changes.jsonl` — one entry per field, carrying both values and the date observed:

```json
{"key":"…|amount","doc":"45500","field":"amount",
 "from":"$2,558,983.00","to":"$21,204,743.00","seen":"2026-08-17"}
```

Before this the database was a photograph: `45500` at the Medical Center reads $2,558,983
expired and $21,204,743 active, the same contract amended, and nothing in the data could
show it had moved. The CSV still holds only the current value — the log is the history.

Read it by folding on `key` and keeping the last entry, never by counting lines: a field
that moves twice appears twice. It is append-only, gitignored, and **cannot be
regenerated from anything** — a night not recorded is gone — so it belongs on the
extraction Release alongside `doc_text.jsonl`.

Nothing on the site reads it yet. Publishing an "amended" flag and the history in the
detail panel is the next step, and wants a few weeks of accumulation first.

`scripts/check_daily_diff.py` fails the week if any entity had more than half its
previously-Active records flip to Expired in a day, skipped below 5 records where the
percentage is noise. That is implausible as real attrition but exactly what a
renamed or retired entity looks like — which `check_entity_drift.py`, run first, is
meant to catch before it gets this far.

### Getting the data back

`data/` holds two things that are expensive rather than impossible to recreate, and both
have a home to fetch them from. Neither is in git.

```bash
# CSVs (363 MB, ~20h of scraping) — cached by the nightly workflow.
gh workflow run ne-contracts-rescue-cache.yml --ref main   # then download its artifact

# Document text and descriptions (1.5 GB, ~36h of downloading) — a Release, because
# nothing else keeps them. doc_text.jsonl is the one that matters; scope.jsonl
# rebuilds from it in about a minute with scripts/extract_scope.py.
gh release download extraction-data-2026-08-17 -D ne-contracts/data
cd ne-contracts/data && gunzip doc_text.jsonl.gz scope.jsonl.gz
```

Both were lost or nearly lost in August 2026 — the CSVs vanished from the working
machine and survived only because CI happened to cache them. **Re-publish the Release
after any large re-extraction.**

---

## How it works

### The scraper

The site splits entities into two categories needing different handling. Higher
Education searches take a Contract/Purchase Order filter and render into a
`table#entitygrid`; State searches have no such filter and render into a
`table#agencygrid`. `scrape.py` branches on both.

Detail pages are fetched on a pool of sessions that have **never run a search**. This is
not incidental: the site keeps search results in server-side session state and serializes
every request touching it, so concurrent fetches on the searching session queue behind
one another. Search-free sessions took a page of 25 detail fetches from ~26s to ~5s —
about 7x over the full job. See `detail_session()` before changing it.

Pacing is set from measurement. A page of 25 records costs one results request plus 25
detail requests, so detail fetches are ~96% of traffic and `MAX_WORKERS`/`DETAIL_DELAY`
are what actually govern load.

- **More workers is not faster.** An A/B against the live site had 15 and 20 workers
  finishing in the same ~3.98s while median detail latency rose 1.15s → 1.42s. The site
  is the bottleneck; extra concurrency only queues. Don't raise it.
- **`PAGE_DELAY` was mostly dead time** — consecutive results requests are already
  separated by the ~2.6s of detail fetching between them. It is now 0.3s.
- **Pipelining** the next results fetch behind the current page's detail fetches bought a
  real 36% (355 → 483 records/min) at no extra load.

Each combo reports its median detail-fetch time and flags a sustained climb past 2.0s.
The healthy baseline is ~0.95s; a run drifting well above that is asking for more than
the site wants to give.

### Why the payload is columns, not JSON

One JSON file of all 739,605 rows is ~104 MB, ~49 MB gzipped, and roughly 380 MB of heap
— minutes to load and an out-of-memory crash on a phone. Packed column-wise the same
values compress about 5x better, because a column of integers is far more compressible
than the same integers interleaved with JSON punctuation.

So the payload is split by access pattern:

| | gzipped, as Pages serves it |
| --- | ---: |
| numeric columns, vendor names, document numbers — **loaded up front** | **6.81 MB** |
| which parser wrote each description (`descsrc.bin`) — **loaded up front** | 0.03 MB |
| link tokens — **fetched on click**, 362 blocks | 23.67 MB raw |
| descriptions — **fetched on click**, 362 blocks | 49.95 MB raw |
| description search index — **fetched only if you tick the box** | 9.92 MB |

`descsrc.bin` is one byte per row and would be a seventh column in `cols.u8.bin` but for
`build_site.py --descriptions-only`, which attaches descriptions to a payload that is
already built and live and deliberately writes no byte of any resident column file. A
column there would have gone stale on that path — descriptions present, every row
reporting it has none, nothing failing. Its own file means whichever path writes the
descriptions writes this beside them, and `verify_descriptions()` fails the build if the
two ever disagree on a row.

It also exploits the structure of the state's URLs: `DT` is determined by document type,
`V` maps 1:1 with vendor, and `A`/`D`/`N` vary together across only 98 distinct
combinations, so each row carries a small index instead of three long tokens. Every URL
is round-trip verified against the original before the payload is written — 1,431,967 of
them — so the compression is lossless.

That verification earns its keep: `A`, `D` and `N` each looked entity-determined at two
entities and still did across five. At 92 the rule breaks — twelve agencies carry more
than one `N`, and three `N` values span agencies. The round-trip check caught it rather
than shipping broken links.

`scripts/ne_format.py` owns the layout and is the only place that knows it. Each file
holds one item size, so a section's offset is `n * itemsize * index` — no header, no
offset table, no padding — and the reader checks every file's length against
`meta.count`.

Measured in-browser over all rows: a keystroke filters in **4–21 ms**, a column's first
sort takes 56–199 ms and is cached after, and peak heap is **~90 MB** (against ~154 MB
for the old payload at 40% of the size). Sorting happens once per column rather than once
per keystroke: the page caches an ordering and filtering walks it.

Loading `/?selftest=1` checks every column against a CRC recorded at build time and
rebuilds 1,000 sampled URLs against addresses taken from the source CSVs. **Run it
against the deployed site, not a local server** — only a real deployment exercises gzip
in transit, and CDN behaviour produced the one design-changing surprise here.

### Publishing

`.github/workflows/pages.yml` builds the payload in CI and deploys it as a **Pages
artifact**. Pages is set to `build_type: workflow`; the site is not served from the
branch. `d/<buildId>/` and `manifest.json` are gitignored — building locally is how you
preview a change, and the result stays on your machine.

| trigger | what happens |
|---|---|
| push to `main` | restores the last payload from the Actions cache and deploys it. No rebuild, so a portfolio edit does not need the CSVs. |
| nightly, on publish day | `ne-contracts-daily.yml` dispatches `pages.yml` **after** `check_daily_diff.py` passes, so it rebuilds from fresh data. |

The dispatch is deliberately not a `workflow_run` trigger. That fires on every successful
nightly, and the guard rail only runs on the publish day — the site would have deployed
unguarded data six days out of seven.

Descriptions are built in CI from `scope.jsonl.gz`, downloaded from the newest
`extraction-data-*` release. `carry_descriptions_forward()` remains as a fallback but
cannot be the main path any more: it reads the build being replaced, and that build is
no longer in the repository.

`index.html` carries no build identity — it reads `manifest.json` with
`cache: 'no-store'` — so a reader holding a stale page can never pair it with a different
build's data.

Why it works this way: under branch publishing the payload *had* to be committed to be
served, and nothing ever left history. `.git` reached 546 MB, growing ~100 MB per
published build, forever. Removing 1,469 files stopped that on 17 Aug 2026. It did not
reclaim the ~450 MB already in history; that needs a force-push and would invalidate
every clone, so it remains a separate decision.

### Nightly automation

`ne-contracts-daily.yml` scrapes all three datasets with `--daily` every night at 10pm
Central. GitHub Actions cron has no DST awareness, so two entries fire daily and the gate
compares `github.event.schedule` against the entry implied by the current offset, letting
exactly one through. It gates on *which entry fired*, never on the wall clock: GitHub's
scheduler routinely runs 30–90 minutes late, and an earlier hour-equals-22 check silently
skipped entire nights, reporting success while scraping nothing. For the same reason the
weekday is rolled back when a run lands before noon Central, so a delayed Sunday-night
run still reaches the publish leg.

**Runners are ephemeral and `data/*.csv` is gitignored**, so `data/` round-trips through
`actions/cache`: restored under a `ne-contracts-data-` prefix, saved under a fresh
run-scoped key (cache keys are immutable, so "latest" only works via prefix-restore plus
a new key per save), with the oldest pruned via `gh cache delete` to stay under the
10 GB/repo cap.

**A fresh clone's first run has no cache and will fail** — `scrape.py --daily` refuses to
treat a missing baseline as "nothing was ever active". Bootstrap once:

1. Commit the existing `data/*.csv` to a **short-lived branch**, commenting out the
   `data/*.csv` line in `.gitignore` for that commit only. Use a branch, not `main` —
   these are ~346 MB and a revert would not remove the objects from history.
2. Dispatch `ne-contracts-rescue-cache.yml` **on that branch**; it republishes `data/` as
   an artifact.
3. Dispatch `ne-contracts-seed-cache.yml` **on `main`**, passing that run's ID as
   `artifact_run_id`.
4. Delete the branch.

**Step 3 must run from `main`.** GitHub scopes each cache to the ref that saved it and
shares it only with that ref or the default branch, so a cache saved on a short-lived
branch is invisible to the nightly and orphaned once the branch is deleted. An earlier
version of this had the whole thing on the branch, and it took the nightly down for two
days with `refusing --daily: data/nu_contracts.csv does not exist` — the seed was intact
the whole time, just permanently out of scope.

---

## Guard rails

Read this before changing anything that writes.

**Descriptions are the state's words, verbatim.** Nothing is generated or summarised.
Typos, boilerplate and truncation that the state itself published all stay.

**Never merge vendors automatically.** `scripts/vendor_groups.json` is the only source of
truth and every entry was read by a human. A wrong merge invents spending that never
happened, which is worse than the fragmentation it would fix. Three properties to keep:
no row ever displays a name the state did not publish; the aggregate is labelled as ours
in those words (*"Grouped by us, not by the state."*); and a rescrape that renames a
vendor fails the build rather than silently shrinking a total.

**Never record an absence you did not observe.** A failed fetch is not evidence that a
contract has no document, that a field is empty, or that a record is gone. A silent
source is the one input that looks identical to "nothing there", and it has now caused
the two worst data failures in this project: blank descriptions attributed to the state,
and every active contract expired at once.

**Put the refusal where the write is.** A check that runs before publishing cannot
protect data that the scrape has already written and cached. `check_daily_diff.py` was
built for exactly the mass-expiry case and never fired, because it guards the weekly
publish leg rather than the daily write. Keep the two
apart in the value itself — `""` for "read it, there is nothing" and `None` for "could
not find out" — and hold back anything unknown rather than writing it. The CSV is a
record of what the state published, so a value in it must be something we actually saw.

Every script that writes needs its own refusal, not one somewhere upstream.
`document_service_healthy()` guarded `scrape.py` from Aug 2026 and not
`extract_text.py`, which fetches documents itself and files two of its four verdicts
permanently. It guards both now, and the canary is imported rather than copied so there
is only one to keep current.

**Check extraction against the source document, not against itself.** Every sanity check
on the description parsers compared output to other output, and all of them passed while
the University parser was truncating 93% of what it read. The bug was found by opening a
PDF and reading it. When you change `extract_scope.py`, pull ten real documents and read
them.

**How to check a description is verbatim:** compare its words against its own document's
text as an *ordered subsequence*, not a contiguous string. 4,000 sampled documents pass
at 100%. The contiguous check reports 82% and every failure is spurious — University
descriptions are reassembled across the money columns by design, so the joined string is
genuinely not contiguous in the source. Reading that 18% as a data problem sends you
chasing a bug that is not there.

**A parse rate is not an accuracy rate.** This project reported "100%" and "97%" parses
for the two purchase-order forms while both returned truncated text. Those numbers only
ever meant "the pattern matched".

**Count documents, not log lines.** `doc_text.jsonl` is append-only and a later entry
supersedes the earlier one, so `wc -l` overstates by every retry. Reading it wrong
produced two false numbers in one session: 40% complete that was really 23%, and 14,551
errors that were really 63, because the retries that fixed them were counted alongside
the failures they replaced. Fold on `tok`, keep the last entry, then count.

**No tracked text file may carry a raw NUL byte**; `tests/test_no_nul_bytes.py` enforces
it. `index.html` carried one for five days — a sort separator written as a raw character
instead of the `\x00` escape. Browsers did not care, but `file` reported the page as
`data`, `grep` matched nothing and exited silently, and **git showed no diffs for the
project's most important file.** Then the paragraph documenting that bug contained the
byte it was describing, so the handoff was itself unsearchable. If a tool suddenly stops
finding text that is plainly there, check for NULs first.

**`meta.incomplete` has now failed twice, both times in the same direction** — claiming
the entire state was uncollected when it was finished. First from `load_progress()`
returning a pair bound to one name; then from moving the build into CI and leaving the
scrapers' checkpoints behind on the laptop. It is the project's most consequential single
value, because it speaks to whether any number on the page can be trusted. A missing
checkpoint now means "unknown" and claims no gaps, and the checkpoints are tracked in git
so a public claim never depends on a file riding in a cache.

**Two things the publish must never do**, both asserted in the staging step: ship
`ne-contracts/data/` (363 MB of scraped CSVs), and deploy a `manifest.json` naming a
build directory that is not in the artifact.

**Never let an empty `data/` reach the cache.** Restore matches on prefix and takes the
newest hit, so one cache saved from an empty directory shadows the good one on every
subsequent run and the failure feeds itself.

---

## Things that bit us

**Ranged HTTP requests are unusable on GitHub Pages.** A range request advertising gzip —
which browsers always do, and `fetch()` cannot override — is served against the
*compressed* representation. Ask for bytes 100–115 and you get 16 bytes of a gzip stream
plus a `Content-Range` denominator that is the compressed length. This is why deferred
data is block files, never byte ranges. Pages *does* gzip `application/octet-stream`, so
raw binary compresses in transit with no client-side work.

**The site's search results live in server-side state that expires.** After ~2,000 pages
of continuous paging every further page returns "No results found", indistinguishable
from the end of the data. Reading it as the end once marked UNL purchase orders complete
at 144,425 of 178,573 records, with no error anywhere. The scraper re-runs the query and
returns to its position; that fired twice during the Medical Center's 9,186 pages and
saved ~170,000 records. **Any long paginated scrape of this site needs that recovery.**

**A `<tr>` cannot be 24 million pixels tall.** The virtual scroller sizes spacer rows to
the full list height, which browsers cap at 2^24 px. At 739,605 rows × 33 px that is
24.4 M and the bottom third of the table silently becomes unreachable — the scrollbar
just stops. The page caps the track at 15 M px and scales scroll position past that.

**Both purchase-order forms wrap their description column, and both parsers stopped at
the first line.** The single worst bug here, and it survived because the documentation
asserted the opposite: that the University's 40-character column *cut* the text and we
were faithfully reproducing the state's truncation. It does not cut, it wraps. A reader
checked document `4740007268` and found the site showing "GENERAL CONSTRUCTION SERVICES
FOR" where the state wrote "GENERAL CONSTRUCTION SERVICES FOR REMODEL OF BOB DEVANTEY
SPORTS CENTER PER UNL INVITATION TO BID 909353-12." on a $15,027,565.88 purchase order.

| | scale of the loss | cause |
|---|---|---|
| University | 54,280 of 58,060 items wrapped; **93% truncated** | read only the line carrying the money columns |
| State agency | **892 of 1,367 tails discarded** | rejected any tail containing a digit |

Neither continuation can simply be appended, for different reasons. On the University
form, PDF extraction does not emit the page in reading order, so the lines under a row
are often the vendor address block and then the table header — `FURNITURE` is the stop
list, built by counting the most common line following an item across 40,000 documents.
On the state form the tail may be the *next row*; the marker is its four-decimal quantity
and unit-price columns, because nobody types "1.0000" into a description and the digit
test that preceded it took `1/2 PINT/CONTAINER, 1%` with it.

Both caps were also doing the cutting themselves — 12 lines bound 26% of University
items, and the state's 80 characters sat below the 99th percentile of genuine tails. They
are now 25 and 400, chosen so they never bind. If either starts binding, the form has
changed and the fix belongs in the stop lists, not the cap.

Three further row-skipping bugs came out of diffing a full re-extract rather than
sampling: four-decimal unit prices (a capacitor order described itself as "SHIPPING", the
one line priced in whole cents), no-charge rows printing one money column instead of two
(an antibody order kept only what it was billed for), and the form being chosen by
whichever pattern fired first — which made University rows start matching inside state
grocery orders once the money pattern widened.

**`A`, `D` and `N` in detail URLs are not entity-determined.** True at two entities and
at five, false at 92. Stored as the 98 combinations that actually occur.

**`load_progress()` returns a pair.** Binding it to one name made every entity test as
uncollected. See the `meta.incomplete` guard rail above — this is half of why it is
there.

**The site renders entity names in caps on state result grids** (`DRY BEAN COMMISSION`)
but the dropdown uses title case. Rows record the canonical dropdown name.

**A two-day outage marked every active contract in the database Expired.** On the night
of 17 Aug 2026 the state's search answered "No results found" for every entity. That is
not a shape `--daily` was built to doubt: `scrape_entity` re-runs the query and resumes
when a page *after* the first comes back empty — the guard that saved 170,000 records
from expiring session state — but an empty *first* page is treated as a clean finish.
So `seen` came back empty, `known - seen` was everything, and one run wrote:

```
Patched 10,358 row(s) Active -> Expired.
Patched 12,233 row(s) Active -> Expired.
Patched 21,472 row(s) Active -> Expired.
```

44,063, which is every active record there was. The run was green and every step
succeeded. Nothing in the data could show it had happened, and nothing could undo it:
`--daily` only ever flips Active to Expired, so there was no path back to the truth.

**A guard rail for exactly this existed and did not fire.** `check_daily_diff.py` fails
the week when an entity loses more than half its active records in a day. It runs
`if: publish == 'true'` — the weekly publish leg. 17 Aug was a Monday. And even on a
Sunday it would have been too late: it gates *publishing*, while the damage is done by
the scrape, which has already rewritten the CSV and saved it to the Actions cache. A
check that runs before publishing cannot protect data that is already written.

So the refusal now lives in the scrape, where the write happens. An entity that returns
nothing while it had active records is refused, as is one where more than half of 20+
active records vanish at once; both re-check next run. Ordinary expiries still flip, and
a small body genuinely clearing out its four contracts still flips — a guard that freezes
the database would be its own bug, so both cases have tests.

**It compounded quietly.** Once every record read Expired, the next night's scrape had no
memory of them: `known_active` was empty, so all 44,063 active contracts looked new and
were written again as fresh rows. The only reason that never reached readers is that
publishing is gated to Sundays, so no build shipped in between. Repaired by deleting the
two poisoned Actions caches and rebuilding from the pre-outage one; the restored counts
came back at exactly 44,063 active and 695,542 expired, which is the local count less the
128 duplicate rows the build drops — that reconciliation is what proved the older cache
was undamaged.

**The End column was blank for every row, from the first build.** A local `var end` for
the virtual scroller's row window shadowed the module-level `end` column, so
`fmtDate(end[id])` read a property off a Number. Three things hid it: the payload was
always correct, the crc32 selftest on `end` passed the entire time because it checks the
payload rather than the page, and the CSV export sits outside that function — so exported
files carried end dates while the table never did. Renamed to `stop`. Both of these were
found by Justin reading the live site, not by any check in this repo.

**The page explained a blank description by guessing, and was wrong a quarter of the
time.** It told readers a missing description was "most likely a scanned image rather
than text". True of 89,180 documents; false for the 29,663 that have a perfectly good
text layer and simply defeated our parsers — 16,736 of those carry over 2,000 characters
of extractable text. As a population statistic the sentence was defensible; standing
under one specific contract it was a false claim about that document, and it also
quietly blamed the state for our own parser's limits.

Spotted by reading the site, not the code: the documents looked like native PDFs. The
page now says only "No description could be read from this document." The cause is
knowable per document — `doc_text.jsonl` records `text` or `scanned` for every one — so
this could be reported precisely rather than dropped, but a plain sentence beats a
confident wrong one, and the breakdown lives here instead (see "Descriptions" above).

**"No document" and "we could not find out" were the same value.** For about two days
from 17 Aug 2026 the state stopped serving documents: `ViewDocument` returned an ASP.NET
error page, and detail pages kept returning HTTP 200 while quietly rendering no link at
all. That is byte-for-byte what a contract with no document attached looks like, and
`get_view_url()` returned `""` for both — so a scrape during the outage would have
recorded "no document" as a fact for every new contract and published a site with
silently missing links. No step would have failed.

Nothing was actually damaged, but only by luck: the state's *search* was down at the same
time, so the nightly found no records to write, and the weekly publish gate meant no
build shipped. A partial outage — search up, documents down — would have poisoned the
data on any publish day.

`get_view_url()` now answers three ways: a URL, `""` for a page read cleanly that offers
nothing (a real fact), and `None` for could-not-tell. A `None` record is held back rather
than written, so it stays unknown to the CSV and the next run asks again. Because the
outage made detail pages look *legitimately* empty, no amount of page parsing can tell
the two apart — so `document_service_healthy()` checks three documents known to carry
files before a scrape starts, and both `--daily` and a full sweep refuse to run when all
three have stopped offering theirs. It is deliberately biased towards "up": a canary that
cannot be fetched proves nothing and is skipped, so rotted canary URLs cannot silently
halt every run. One request when the state is healthy.

**Not one of the 32,227 "unavailable" documents was ever a 404.** `extract_text.py`
filed a document as unavailable — the state has no file, never asked about again — on
either a 404 or a 200 whose body was not a PDF. Only the second ever happened. Every one
of those verdicts was reached from a 200, 17,991 of them `image/tiff`: real scans the
state does hold and we could not parse. The corpus reported "the state has no file to
serve" for 32,227 documents, and the run summary printed those words, without the state
having once said so.

Found while adding the health gate below. The classifier now separates a 404
(`unavailable`) from a file we cannot read (`unsupported`, not retried but not an
absence either) from anything that is not a file at all (`error`, retryable), and
`--retry-non-pdf` re-asks the ones already on disk. It also keeps a hash and the first
300 characters of any non-PDF body, because the 14,185 HTML answers already recorded
cannot be told apart from an outage page — nothing kept the body.

**The state's document service went down again on 22 Aug 2026, and `extract_text.py`
had no guard.** Every `ViewDocument` request returned HTTP 200 with an HTML error page:
*"An internal error occured: An error occurred within the Unity API: The type
initializer for 'Hyland.Core.CoreUtility' threw an exception."* `document_service_healthy()`
had guarded `scrape.py` since the August outage, but `extract_text.py` fetches documents
on its own and was never taught the lesson.

A run started that morning would have recorded a permanent absence for every document it
touched. Worse, `--retry-errors` — the pass most worth running, over 14,586 recoverable
documents — would have spent that verdict on exactly those. The gate is now in front of
the extraction loop, one request before the run and nothing written until it answers,
and `run_extraction_cycles.sh` stops rather than spinning because it runs under `set -e`.

Nothing was damaged, again by luck: the outage happened on a day nobody was extracting.
That is twice now that the thing standing between this project and poisoned data was the
calendar.

**`write_payload()` copies the meta dict it is given.** The search index's `wordCount`
and the vendor groups are added afterwards, so on a full build they never reached
`meta.json` — only `--descriptions-only` rewrote the file at the end, which is the sole
reason the published build ever had them. Every full build was shipping with vendor
grouping silently absent. `meta.json` is now written last and re-read to confirm.

---

## Open work

**~3,021 unreviewed vendor clusters**, holding most of the $11.02 B above.
`scripts/suggest_vendor_groups.py` proposes candidates ranked by spend and **decides
nothing**; a grouping exists only once somebody writes it into
`scripts/vendor_groups.json`. The money is concentrated, so a few dozen decisions cover
most of the error. This is the highest-value unfinished work.

**89,180 scanned documents** — 42.9% of contracts — have no text layer, and no parser
reaches them. This is the difference between 94.8% description coverage of *readable*
documents and ~95% of *all* of them. An OCR pilot is scoped but not run: 500 known-scanned
documents through Tesseract, accuracy measured on descriptions specifically rather than
text generally, cost and wall-clock per thousand, compared against one cloud OCR.

**Change tracking records but does not publish.** `--daily` now writes every amendment
to `data/changes.jsonl` (see "Daily updates"). Nothing on the site reads it yet: an
`amended` flag and change count as resident columns, the history itself as deferred
blocks reusing the `write_desc_blocks` pattern, and a filter beside the existing
data-quality ones. That wants a few weeks of accumulation before it says anything
worth showing.

**29,663 readable documents have no description and mostly never will.** This closes a
line of investigation rather than opening one: 83% carry no numbered heading, no summary
and no line items, because they are email threads, signature pages, notices to proceed
and change-order stubs. There is no statement of the work in them for anything to find,
an LLM included. Every candidate pattern measured together reached 5.4%. Three were
tried and rejected: a "Project:" line (177 documents), an "engages" clause (647, all
inside the SERVICES ones already parsed), an "RE:" line (21, mostly email subjects —
a different thing wearing a description's clothes). **Do not spend a week here.**

**19% of state agency documents do not exist** — the state's own viewer has no file. No
method reaches those rows, ever.

**~450 MB of superseded payloads remain in git history.** Publishing no longer adds to
it, but reclaiming what is there needs a force-push that invalidates every clone.

---

## License

MIT for the code. The underlying records are public data from the Nebraska Department of
Administrative Services.
