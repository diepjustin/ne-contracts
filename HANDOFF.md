# Handoff

Where this project stands, what is safe to touch, and what the data taught us
that is expensive to rediscover. Read the "Guard rails" section before running
anything that writes.

**Live:** https://diepjustin.github.io/ne-contracts/
**Plan for the work in flight:** `~/.claude/plans/effervescent-finding-penguin.md`

---

## 1. What this is

A scraper and a static searchable site for Nebraska state spending, built from
the [State Contracts Database](https://statecontracts.nebraska.gov/Search), the
public database mandated by Neb. Rev. Stat. § 84-602.04. No backend, no build
step for the page itself: `index.html` is self-contained and reads a payload
built by `scripts/build_site.py`. Pushing to `main` publishes.

## 2. Current state

**Scraping is finished.** 738,195 records across all 92 entities — 83 state
agencies, boards and commissions, plus all nine university and state college
campuses. Sitting in `data/*.csv` (gitignored, 362 MB).

**The live site publishes all 738,195**, from build `20260811-215819`. All
four stages of the payload replacement are done and pushed:

| Stage | Commit | State |
|---|---|---|
| 0 — columnar refactor of the builder | `3e1046a` | byte-identical output verified |
| 1+2 — binary payload + widened verify | `ddba1f5` | verified from disk |
| 3 — rewrite the `index.html` data layer | `73a543a` | shipped as `staging/`, verified in a browser |
| 4 — staged rollout, then promote | `73a543a`, then the promotion commit | live |
| 5 — vendor grouping for search and totals | `793b887` | committed 16 Aug, **not yet pushed** |

Measured, and re-verified on the CDN after deploying:

| | |
|---|---|
| resident payload, gzipped as Pages serves it | **6.94 MB** (was 16 MB for 40% of the data) |
| deferred link tokens | 25.45 MB, 361 blocks, fetched on click |
| a keystroke over all 738,195 rows | **4–21 ms** |
| first sort of a column (cached after) | 56–199 ms; amount, sorted at load, is 4 ms |
| peak JS heap | **~90 MB** (was ~154 MB for 40% of the data) |
| `?selftest=1` on the live CDN | 15/15 pass, incl. 1,000 URLs rebuilt in JS matching the scraped CSVs |

Every published file is served `content-encoding: gzip`, checked by hand.

**The cutover cleanup is done** (16 Aug). `data.json`, `staging/` and
`prototype-search/` are gone from the tree. The cached-reader window that
`data.json` existed to cover is `max-age=600` — ten minutes — and it closed four
days before they were removed.

## 3. Guard rails

**`d/<buildId>/` and `manifest.json` are the published payload, and they are
no longer in git** (17 Aug 2026). They are gitignored. `python3
scripts/build_site.py` still writes them locally — ~3 min, ~1.7 GB peak memory
— which is how you preview a change before it ships, and the result stays on
your machine.

Publishing moved to `.github/workflows/pages.yml`, which builds the payload in
CI and deploys it as a **Pages artifact**. Pages is set to `build_type:
workflow`; it is not served from the branch any more. The portfolio site at the
repo root goes out through the same workflow, so a push publishes in a couple
of minutes rather than instantly.

Why: under branch publishing the payload *had* to be committed to be served.
Retiring superseded build directories kept the working tree small, but nothing
ever left history — `.git` hit 546 MB and grew ~100 MB per published build,
forever. Removing 1,469 files stopped that. It did **not** reclaim the ~450 MB
already in history; that needs a force-push and would invalidate every clone,
so it remains a separate decision.

How a build reaches the site:

| trigger | what happens |
|---|---|
| push to `main` | restores the last payload from the Actions cache and deploys it. No rebuild, so a portfolio edit does not need the CSVs. |
| nightly, on publish day | `ne-contracts-daily.yml` dispatches `pages.yml` **after** `check_daily_diff.py` passes, which rebuilds from fresh data. |

The dispatch is deliberately not a `workflow_run` trigger. That fires on every
successful nightly, and the guard rail only runs on the publish day — the site
would have deployed unguarded data six days out of seven.

Descriptions build properly in CI now rather than being carried forward: the
workflow downloads `scope.jsonl.gz` (41 MB) from the newest `extraction-data-*`
release. `carry_descriptions_forward()` remains as the fallback, but it can no
longer be the main path, because it reads the build being replaced and that
build is not in the repository.

**Two things the publish must never do**, both asserted in the staging step:
ship `ne-contracts/data/` (363 MB of scraped CSVs), and deploy a `manifest.json`
naming a build directory that is not in the artifact.

`workflow_dispatch` on the nightly takes a **`dry_run`** input that runs the
drift check and guard rail without dispatching a deploy.

**No tracked text file may carry a raw NUL byte**, and `tests/test_no_nul_bytes.py`
now enforces it across every tracked non-binary file. `index.html` carried one
from Aug 11 to Aug 16 — a sort separator written as a raw character instead of
the `\x00` escape. Browsers did not care and the sort was correct, but `file`
reported the page as `data`, `grep` matched nothing in it and exited silently,
and **git showed no diffs for the project's single most important file.** It
cost two false statements to the user about whether a deploy had landed.

Then the paragraph you are reading did it again: the sentence above describing
the bug contained the byte it was describing, so this handoff was itself
unsearchable until 16 Aug. Both were written by editing scripts that passed the
byte through where the four characters spelling it were meant. **If a tool
suddenly stops finding text that is plainly there, check for NULs first** — and
if you are writing a NUL-handling fix with a script, check the fix too.

**Check extraction against the source document, not against itself.** Every
sanity check on the description parsers compared their output to other output,
or to a regex, and all of them passed while the University parser was
truncating 93% of what it read. The bug was found by opening a PDF and reading
it. Tests prove a parser does what you think; only the source proves that what
you think is right. When you change `extract_scope.py`, pull ten real documents
and read them.

**How to check descriptions are verbatim.** Compare each description's words
against its own document's text as an *ordered subsequence*, not as a
contiguous string. 4,000 sampled documents pass at 100%: every word appears in
the source, in order, nothing invented.

The contiguous check reports 82% and every "failure" is wrong. University
descriptions are reassembled across the money columns by design -- the item
line, then the price, then the wrapped continuation -- so the joined string
genuinely is not contiguous in the source. Reading that 18% as a data problem
would send you chasing a bug that is not there.

**A parse rate is not an accuracy rate.** This document reported "100%" and
"97%" description parses for the two purchase-order forms while both were
returning truncated text. Those numbers only ever meant "the pattern matched" —
they say nothing about whether what it captured was the whole field. Do not
quote them as a quality measure.

**Don't re-scrape casually.** A full run is 20+ hours against a government
server. The CSVs on disk are complete and checkpointed.

## 4. Running things

```bash
cd ne-contracts
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt

python3 scripts/scrape.py contract          # -> data/nu_contracts.csv
python3 scripts/scrape.py purchase-order    # -> data/nu_purchase_orders.csv
python3 scripts/scrape.py state             # -> data/state_agencies.csv
python3 scripts/scrape.py state --status    # progress, no network
python3 scripts/scrape.py state --hours 3   # stop cleanly after 3h
python3 scripts/check_entity_drift.py       # has the state's entity list changed?

python3 scripts/build_site.py                    # binary payload -> d/<buildId>/
python3 scripts/build_site.py --emit-json out.json  # legacy single-file payload
python3 scripts/serve_site.py                    # preview at 127.0.0.1:8765
```

**Getting the data back.** `data/` holds two things that are expensive rather
than impossible to recreate, and both have a home to fetch them from:

```bash
# CSVs (362 MB, ~20h of scraping) — cached by the nightly workflow.
gh workflow run ne-contracts-rescue-cache.yml --ref main   # then download its artifact

# Document text and descriptions (1.5 GB, ~36h of downloading) — a Release,
# because nothing else keeps them. doc_text.jsonl is the one that matters;
# scope.jsonl rebuilds from it in 30s with scripts/extract_scope.py.
gh release download extraction-data-2026-08-17 -D ne-contracts/data
cd ne-contracts/data && gunzip doc_text.jsonl.gz scope.jsonl.gz
```

Both were lost or nearly lost in August 2026 — the CSVs vanished from this
machine entirely and survived only because CI happened to cache them. Neither is
in git; `data/*.csv`, `doc_text.jsonl` and `scope.jsonl` are all gitignored on
purpose. Re-publish the Release after any large re-extraction.

With the preview running: `/staging/` is the new page, `/` is the live one.
`/staging/?selftest=1` checks every column against the digests in `meta.json`
and rebuilds 1,000 sampled URLs against the addresses in `selftest.json`,
which come from the scraped CSVs rather than from the payload.

Every scrape checkpoints per page and resumes; interrupting is safe.

## 5. Measured facts — do not re-derive

Each of these cost real time or a live experiment.

**Payload composition** (738,195 rows, gzipped as Pages serves it):

| | gzipped |
|---|---|
| numeric columns + vendor names + doc numbers — **resident** | **6.86 MB** |
| DN + view + vendor tokens — **deferred**, 361 blocks of 64 KB | 25.45 MB |
| the same data as one JSON file | 38 MB |

Columns compress ~5× better than the same numbers interleaved with JSON
punctuation. The resident set is *smaller than the 16 MB the site ships today
for 40% of the data.*

**Scraper pacing.** A page of 25 records costs 1 results request plus 25 detail
requests, so detail fetches are ~96% of traffic. Measured latency: ~950 ms per
detail page, ~1.4 s per results page.
- `MAX_WORKERS` 15 → 20 gave **identical** wall time and pushed median latency
  1.15 s → 1.42 s. The site is the bottleneck; more concurrency only queues.
  Don't raise it.
- Pipelining the next results fetch behind the current page's detail fetches
  bought a real **36%** (355 → 483 records/min) at no extra load.

**Data shape.**
- 0 rows lack a detail URL; 47,050 (6.37%) lack a view URL.
- Doc-number alphabet is exactly `-.0123456789ACDEFGHILNOPRSTUVWYZ` — uppercase
  only, missing B/J/K/M/Q/X. Stage 3 uses this to skip the doc scan entirely
  when a query contains a character no document number can hold.
- DN decodes to 16 bytes except **3 rows** (32 bytes), which travel in
  `meta.dnExceptions`. Every view token is exactly 16 bytes.
- 59,875 distinct vendors, 34 type codes, 92 entities, 98 `(A,D,N)` triples.

## 6. Things that bit us

**Ranged HTTP requests are unusable on GitHub Pages.** A range request that
advertises gzip — which browsers always do, and `fetch()` cannot override — is
served against the *compressed* representation. Ask for bytes 100–115 and you
get 16 bytes of a gzip stream plus a Content-Range denominator that is the
compressed length. This is why deferred data is split into block files. It also
probably explains the unresolved latency in `prototype-search/`: sql.js-httpvfs
has been reading corrupted ranges this whole time.

Pages *does* gzip `application/octet-stream`, so raw binary compresses in
transit with no client-side work.

**The site's search results live in server-side state that expires.** After
~2,000 pages of continuous paging, every further page returns "No results
found". The scraper originally read that as end-of-results, marked the combo
complete, and moved on — UNL purchase orders "finished" at 144,425 of 178,573
records with no error. It now re-runs the query and returns to where it was;
this fired twice on the Medical Center's 9,186 pages and saved ~170,000
records. **Any long paginated scrape of this site needs that recovery.**

**`A`, `D` and `N` in detail URLs are not entity-determined.** That held at two
entities and again across five, then broke at 92 — twelve agencies carry more
than one `N`, and three `N` values span agencies. They are now stored as the 98
combinations that actually occur, with an index per row. `verify()` caught this
rather than shipping broken links; keep it running.

**A `<tr>` cannot be 24 million pixels tall.** The virtual scroller sizes
spacer rows to the full list height, which browsers cap at 2^24 px (Chrome).
At 295,895 rows that is 9.8 M px and fine; at 738,195 it is 24.4 M and the
bottom third of the table silently becomes unreachable — the scrollbar just
stops. `staging/index.html` caps the track at 15 M px and scales scroll
position onto the real list past that point. Anything that changes `ROW_H` or
the row count has to keep that in mind.

**`load_progress()` returns a pair.** `build_site.py`'s `incomplete_coverage()`
bound `(done, partial)` to one name, so every entity tested as uncollected and
`meta.incomplete` listed all 101 of them as "still being collected" — the
page's honesty note inverted into a false alarm. Fixed; the correct answer is
now an empty list. It only surfaced because the published `data.json` predates
the scraper adding page-position tracking.

**The site renders entity names in caps on state result grids**
(`DRY BEAN COMMISSION`) but the dropdown uses title case. Rows record the
canonical dropdown name, because both the resume checkpoint and
`build_site.py`'s entity guard match on that exact string.

**Purchase orders are not a Higher Education concept**, even though the site's
own filter only offers them there. Ten of the 32 agency type codes are filed
under "Purchase Orders" — 152,210 records, ~65% of agency data. The dataset is
177,301 purchase orders to 118,594 contracts, close to the reverse of what the
interface implies. `scripts/type_groups.json` carries the mapping and records
its own dissent: code `PO`, which the site's Purchase Order search returns, has
a detail page headed "Contracts". The map overrides that.

## 7. Data caveats worth knowing as a journalist

- **The database is not a complete record of state spending, and the gaps are
  not random.** The state's own
  [FAQ](https://das.nebraska.gov/materiel/contract-database/faq.html) excludes
  contracts from HHS, the University, State Colleges, Veterans' Affairs,
  Education, the Commission for the Blind, and NIFA that provide aid to a
  specific individual. Several of the largest agencies are undercounted. Do not
  read an agency total as its budget.
- **A few records carry billion-dollar values** and will dominate any sum. The
  largest is `95601`, HHS / "CREIGHTON UNIVERSITY - ALL PAYMENTS", at
  $38,025,000,000. Some are plausibly real not-to-exceed ceilings on multi-year
  programs rather than errors. Treat the top of the amount column as ceilings,
  not money spent.
- **Only documents active on or after 1 Jan 2014 are in the database at all.**
- **The state updates daily** (per its FAQ).
- The state's own entity list contains a typo — "Deaf & Hard of Dearing" —
  preserved verbatim because matching the source exactly is what makes the
  links work.

## 8. Known open items

**Re-run `?selftest=1` after every rebuild**, against the deployed URL rather
than a local server. It is the only check that covers the JavaScript decoder,
and only a real deployment exercises gzip in transit.

**Vendor names are fragmented. The machinery to group them is built; most of
the reviewing is not done.** Measured across all 738,195 rows: **26 spellings
of Hausmann Construction total $1.40 B, where the site showed $903 M** under
its largest single name. Kiewit is short $222 M across 4 spellings, Hawkins $74
M across 3. About 9,000 vendor strings sit in a cluster with at least one
other, holding **$11.02 B** between them. Until they are grouped the database
gives a *wrong answer* to the most obvious question a reporter brings to it.

Shipped 16 Aug (`793b887`): `scripts/vendor_groups.json` is the only source of
truth and every entry in it was read by a human. `scripts/suggest_vendor_groups.py`
proposes candidates ranked by spend and **decides nothing** — nothing it prints
changes the site. Four groups are seeded; **~3,021 clusters are still
unreviewed**, and reviewing them by spend descending is the open work, because
a few dozen decisions cover most of the error.

Three properties to preserve if you touch this:

- **No row ever displays a name the state did not publish.** Rows keep the
  exact recorded string, because that is what makes a row quotable. The
  grouping only powers search and one aggregate line.
- **The aggregate is labelled as ours**, in those words, in the detail panel:
  *"Grouped by us, not by the state."* It must never read as a state figure.
- **A rescrape that renames a vendor fails the build.** `build_site.py` exits
  if any spelling in the file is missing from the data, rather than silently
  dropping it from its group. Verified by planting a stale spelling.

A wrong merge invents spending that never happened, which is worse than the
fragmentation. So nothing merges automatically, and nothing should.

**`prototype-search/` was retired** (16 Aug). A chunked SQLite full-text index
over 16,467 documents, 504 MB committed, never linked from anywhere, with query
latency that was never explained — most likely the range-request bug above,
since sql.js-httpvfs reads byte ranges and Pages corrupts them. The description
index now covers the same ground for the documents that matter, resident-free
and without a 504 MB payload.

Removed from the tree only. The objects stay in history, so `.git` is unchanged
at ~435 MB; reclaiming that needs a history rewrite and a force-push, which was
judged not worth it. The local `search.sqlite` is gitignored and still on disk
if anyone wants to revive it; `scripts/chunk_search_db.py` and
`scripts/build_search_index.py` are still here and still work.

**"Scope of work" column** was requested by another reporter, and it is mostly
not the AI project it was written up as. The state publishes no such field, so
it has to come from inside the PDFs — but most documents already contain one,
written by whoever filed them, and `scripts/extract_scope.py` lifts it verbatim.
Nothing is generated; every string is the state's own words, which is what makes
it quotable.

The earlier estimate here ("~47% are scanned … an LLM pass over 150,000+
documents") came from a contracts-only sample and does not describe the corpus,
which is 84% purchase orders. Measured instead:

These are *parse* rates -- the pattern matched -- not accuracy rates. See the
guard rail above: both purchase-order columns were parsing at ~100% while
returning truncated text.

| | text layer | description parses |
|---|---|---|
| University purchase orders | 98.4% | 100% |
| State agency purchase orders | 68% of the 81% that exist at all | 97% |
| University contracts | 38.7% | 34% (those with a P2P cover sheet) |

The two forms need two patterns and always will. Both are in
`extract_scope.py`; do not try to unify them.

**Both of them wrap, and both parsers used to stop at the first line.** This
was the single worst bug in the project and it survived because this document
asserted the opposite — that the University's 40-character column *cut* the
text and that we were faithfully reproducing the state's own truncation. It
does not cut. It wraps. A reader checked document `4740007268` against the
source and found the site showing "GENERAL CONSTRUCTION SERVICES FOR" where the
state wrote "GENERAL CONSTRUCTION SERVICES FOR REMODEL OF BOB DEVANTEY SPORTS
CENTER PER UNL INVITATION TO BID 909353-12." on a $15,027,565.88 purchase
order. Fixed 16 Aug in `40d7fe6` (University) and `268a62e` (state).

| | scale of the loss | why it happened |
|---|---|---|
| University | 54,280 of 58,060 items wrapped; **93% were truncated** | read only the line carrying the money columns |
| State agency | **892 of 1,367 tails discarded** | rejected any tail containing a digit |

Neither continuation can simply be appended, and the reasons differ:

- **University:** pdf extraction does not emit the page in reading order, so
  the lines under a row are often the vendor address block and then the table
  header. `FURNITURE` is the stop list, built by counting the most common line
  following an item across 40,000 documents.
- **State:** the tail may be the *next row* rather than this row's
  continuation. The marker is its four-decimal quantity and unit-price columns
  — `ROW_COLUMNS` — because nobody types "1.0000" into a description, and the
  digit test that preceded it took `1/2 PINT/CONTAINER, 1%` with it.

Both caps were also doing the cutting themselves, which is the same error in
miniature: 12 lines bound 26% of University items, and the state's 80
characters sat below the 99th percentile of genuine tails. They are now 25 and
400, chosen so that they never bind — if either starts binding again the form
has changed, and the fix belongs in the stop lists, not in the cap.

**What is kept:** everything that is not furniture, verbatim. That includes
invoicing boilerplate and change-order logs, which read as noise. Document
`4740018436` is why: it puts "-Scope of Work - Director's office update
installation" *after* the boilerplate, so a filter aimed at the noise would
take the substance with it.

The cover-sheet parser was audited for the same class of bug on 16 Aug and does
not have it — 1,556 of 1,568 parse, boundaries land on the next form field, and
no page footer appears inside a description across the sample. Do not re-derive
this.

**19% of state agency documents do not exist** — the state's own viewer has no
file. No method reaches those rows, ever.

Where an LLM would earn its place is the residue: ~6,000 readable contracts on a
plain Standard Agreement template with no summary field. Around $30–100 through
the Batch API, and clearly labelled as machine-written if it ships, because it
is the one part that would not be the state's words.

That delivery decision is made and shipped: descriptions load **on demand** as
deferred blocks, and searching them is opt-in behind the "also search
descriptions" checkbox, which fetches an 8.6 MB inverted index only when a
reader ticks it. The resident payload stays 6.94 MB for everyone else.

The download is done for purchase orders and running for contracts. Measured on
disk 16 Aug, **625,632 documents** across 640,152 log lines:

| | | |
|---|---|---|
| text | 538,876 | 86.1% — a real text layer, parsed |
| scanned | 61,001 | 9.8% — opened fine, no text in it |
| unavailable | 25,692 | 4.1% — the state has no file to serve |
| error | **63** | 0.0% — neither parser could open it |

**Count documents, not lines.** The log is append-only and a later entry for a
token supersedes the earlier one, so `wc -l` overstates by the 14,520 retry
lines. Reading it any other way produced two wrong numbers in one session: a
40%-complete claim that was really 23%, and a 14,551-error figure that was
really 63 because the retries that fixed them were counted alongside the
failures they replaced. Fold on `tok`, keep the last entry, then count.

**The contract errors were ours, not the state's, and are fixed.** Two bugs ran
during the first contract attempts: the pdfminer fallback raised
`NameError: pdfminer_text` because the import never landed, and `cryptography`
was missing for AES-encrypted files. At their peak they were erroring 55% of
contracts. With both fixed the rate is 8 in 20,120 — 0.04% — and a
`--retry-errors` pass recovered all but 63 of the historical ones.

Errors will not retry themselves: `extract_text.py` counts a recorded error as
done, deliberately, so a resumed run does not re-fetch them. `--retry-errors`
is the switch, and `unavailable` is excluded from it on purpose — the state has
no file for those and re-asking will not change that.

**Coverage will be uneven and readers cannot see why.** Excellent for University
contracts, terse for purchase orders, absent for scans and for documents the
state never published. A blank cell reads as "this contract has no description"
when it means "we could not read the PDF" — the same honesty problem
`meta.incomplete` solves for agency coverage, and it needs the same kind of
answer on the page.

## 9. Daily automation

`.github/workflows/ne-contracts-daily.yml` scrapes all three datasets with
`--daily` every night (10pm Central; GitHub Actions cron has no DST
awareness, so two cron entries fire daily and the gate step compares
`github.event.schedule` against the entry implied by the current
`TZ=America/Chicago date +%z` offset, letting exactly one through). Note it
gates on *which entry fired*, not on the wall clock: GitHub's scheduler
routinely runs 30–90 minutes late, and the original hour-equals-22 check
silently skipped entire nights when both entries drifted past 22 — reporting
success while scraping nothing. For the same reason the weekday is rolled
back a day when a run lands before noon Central, so a delayed Sunday-night
run still reaches the publish leg. On Sundays only, it
additionally runs `check_entity_drift.py` → `build_site.py` →
`check_daily_diff.py` → commit + push, in that order, so any one of those
failing blocks the publish and fails the workflow, which triggers GitHub's
built-in scheduled-run failure email. See `README.md`'s "Daily updates"
section for how `--daily` itself works.

**GitHub Actions runners are ephemeral and `data/*.csv` is gitignored**, so
there is no persistent state across nightly runs by default — every run
would look like a cold start with nothing to diff against. `data/` round-
trips through `actions/cache` instead: restored under a `ne-contracts-data-`
prefix at the start of a run, saved under a fresh run-scoped key at the end
(cache keys are immutable in GitHub Actions, so "latest" only works via
prefix-restore + a new key per save), with the oldest entries pruned via
`gh cache delete` after each save to stay well under the 10GB/repo cap.

**This means a fresh clone's first workflow run has no cache to restore and
will fail** — `load_known_active()` returns `None` and `scrape.py --daily`
refuses to run rather than treat a missing baseline as "nothing was ever
active." Bootstrap it once:

1. Commit the existing `data/*.csv` to a short-lived branch, commenting out
   the `data/*.csv` line in `.gitignore` for that commit only. Use a branch,
   not `main` — these are ~346 MB and a revert would not remove the objects
   from history; deleting an unmerged branch lets them be collected.
2. Dispatch `ne-contracts-rescue-cache.yml` **on that branch**. It notices the
   checkout already carries the CSVs, skips its restore, and republishes
   `data/` as an artifact.
3. Dispatch `ne-contracts-seed-cache.yml` **on `main`**, passing that run's ID
   as `artifact_run_id`. This is the step that must run from `main` — see
   below.
4. Delete the branch.

Every run after that restores from the seeded cache normally.

**Step 3 must run from `main`.** GitHub scopes each cache to the ref that
saved it and shares it only with that ref or the default branch, so a cache
*saved* on a short-lived branch is invisible to the nightly run and is
orphaned outright once the branch is deleted. Staging the CSVs on a branch is
fine — that is step 1 — but the `cache/save` has to happen on `main`, which is
why the artifact hop in steps 2–3 exists. An earlier version of this section
had the whole thing on the branch, and it took the nightly job down for two
days in Aug 2026 with `refusing --daily: data/nu_contracts.csv does not
exist` — the seed was intact the whole time, just permanently out of scope.
Recovering from that is the same artifact hop: re-create the branch by name
(cache scope matches on the ref string, so the orphaned cache comes back into
view), dispatch `ne-contracts-rescue-cache.yml` on it, then feed that run's ID
to `ne-contracts-seed-cache.yml` on `main`.

**Never let an empty `data/` reach the cache.** Restore matches on the
`ne-contracts-data-` prefix and takes the *newest* hit, so a single cache
saved from an empty directory shadows the good one on every subsequent run
and the failure feeds itself — which is how two days of outage became
self-sustaining above. The "Check data/ is worth caching" step gates both the
save and the prune on CSVs actually being present, and the prune is deliberately
downstream of a *successful* save so a run that banked nothing can never age
out the baseline it failed to replace.

**Guard rail thresholds** (`scripts/check_daily_diff.py`): an entity fails
the week if more than 50% of its previously-Active records flipped to
Expired in a single day, skipped below 5 previously-Active records where the
percentage is noise. Chosen because a renamed/retired entity's daily scrape
returns "No results found" on page one — `seen` is empty, 100% of `known`
flips, always over threshold regardless of entity size — which is exactly
the failure mode `check_entity_drift.py` (run first in the Sunday leg) is
meant to catch before it gets this far.

**What `--daily` deliberately does not catch:** an amendment to an existing
Active record's Amount, Vendor, or End Date. It's seen again, matched as
already-known by `Detail URL`, and skipped — no rewrite. Only a full
re-scrape (`scrape.py contract`/`purchase-order`/`state`, no `--daily`)
picks up amendments. Worth an occasional manual full re-scrape; not
automated.
