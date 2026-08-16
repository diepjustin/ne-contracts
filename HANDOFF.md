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

**One cleanup left, deliberately deferred.** `data.json`, `staging/` and any
prior `d/<buildId>/` are still in the tree. Pages serves `index.html` with
`max-age=600`, so for ten minutes after the cutover a reader could hold the old
page — which keeps fetching `data.json` and keeps working. Delete all three in
a separate commit after a week or so. Nothing depends on them.

## 3. Guard rails

**`d/<buildId>/` and `manifest.json` are the published payload.** ~50 MB per
build, and they are committed. `python3 scripts/build_site.py` writes a new
build directory and repoints `manifest.json` (~3 min, ~1.7 GB peak memory);
both must be committed together, and the old build directory deleted in a
later commit, not the same one. Published bytes are ~52 MB per build and
`.git` is already over 300 MB, so don't rebuild for nothing.

**`data.json` is dead weight kept on purpose.** The 295,895-row payload the
old page read. It exists only so a reader holding a cached copy of the old
`index.html` keeps working through the cutover window; see §2.

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

**The cutover cleanup**, a week or so after the promotion: delete `data.json`,
`staging/`, and any superseded `d/<buildId>/`. One commit, no code changes.
This is the only thing left from the payload work.

**Re-run `?selftest=1` after every rebuild**, against the deployed URL rather
than a local server. It is the only check that covers the JavaScript decoder,
and only a real deployment exercises gzip in transit.

Keep `data.json` through the cutover. Pages serves `index.html` with
`max-age=600`, so a reader can hold a ten-minute-stale page; the old page keeps
fetching `data.json` and works, a fresh page fetches the binary payload and
works, and neither can pair with the other's data because `index.html` carries
no build identity — it reads `manifest.json`.

`README.md` still publishes the old size, timing and verification numbers.
Update it as part of the promotion commit.

**Vendor names are fragmented.** 6,742 vendor strings collapse to 1,987 real
companies — the state records the same firm many ways, often with a contract
number appended (`BOCKMANN, INC`, `BOCKMANN, INC. 14721`, `BOCKMANN INC.` …
174 spellings). The site therefore reports Bockmann's largest contract as
$4.5 M when the real total is $10.4 M. This is the highest-value unbuilt
improvement: it is what makes "how much has Nebraska paid this company?"
answerable. Do it as an inspectable mapping file, like `type_groups.json`, not
blind normalization.

**Full-text search** (`prototype-search/`) is live but unlinked, covering
16,467 documents. Query latency is unresolved — see the range-request finding
above for a likely cause. Do not extend it to the new corpus before that is
understood.

**"Scope of work" column** was requested by another reporter, and it is mostly
not the AI project it was written up as. The state publishes no such field, so
it has to come from inside the PDFs — but most documents already contain one,
written by whoever filed them, and `scripts/extract_scope.py` lifts it verbatim.
Nothing is generated; every string is the state's own words, which is what makes
it quotable.

The earlier estimate here ("~47% are scanned … an LLM pass over 150,000+
documents") came from a contracts-only sample and does not describe the corpus,
which is 84% purchase orders. Measured instead:

| | text layer | description parses |
|---|---|---|
| University purchase orders | 98.4% | 100% |
| State agency purchase orders | 68% of the 81% that exist at all | 97% |
| University contracts | 38.7% | 34% (those with a P2P cover sheet) |

The two forms need two patterns and always will: the University's has a
40-character fixed-width description column, the state's has none but wraps the
text past the money columns, so a row's continuation is the text between it and
the next row. Both are in `extract_scope.py`; do not try to unify them.

**19% of state agency documents do not exist** — the state's own viewer has no
file. No method reaches those rows, ever.

Where an LLM would earn its place is the residue: ~6,000 readable contracts on a
plain Standard Agreement template with no summary field. Around $30–100 through
the Batch API, and clearly labelled as machine-written if it ships, because it
is the one part that would not be the state's words.

Remaining work is a ~3-day paced download of the other 660,000 documents
(`scripts/extract_text.py`, resumable), then a delivery decision: descriptions
for every row are ~30 MB against a 6.94 MB resident payload, so they either load
on demand — viewable but **not searchable** — or the page gets several times
heavier. Searching them is most of their value to a reporter, so that trade-off
is the real design question, not the extraction.

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
