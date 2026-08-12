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

**The live site publishes 295,895 of them** as a single 35 MB `data.json`
(16 MB gzipped). That is the pre-purchase-order corpus and it works fine.

**In flight:** replacing that payload so the full 738,195 can ship. Two of four
stages are done and pushed:

| Stage | Commit | State |
|---|---|---|
| 0 — columnar refactor of the builder | `3e1046a` | done, byte-identical output verified |
| 1+2 — binary payload + widened verify | `ddba1f5` | done, verified from disk |
| 3 — rewrite `index.html` data layer | — | **not started** |
| 4 — staged rollout, then promote | — | not started |

Neither pushed commit changes anything published — they only touch build
scripts.

## 3. Guard rails

**`data.json` in the tree is the published file.** It is the 295,895-row
payload the live site serves right now. `build_site.py` no longer writes it
(binary is the default; `--emit-json PATH` writes the legacy format
elsewhere), but do not commit a rebuilt `data.json` before Stage 4 — the
current `index.html` cannot read the 738k version, and the page and payload
must land together.

**`d/<buildId>/` and `manifest.json` are generated and uncommitted.** ~50 MB.
They publish in Stage 4 alongside the new page. Regenerate with
`python3 scripts/build_site.py` (~4 min, ~1.7 GB peak memory).

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

**Next up: Stage 3**, rewriting `index.html`'s data layer. Full detail in the
plan file; the parts that need care are `orderFor()` (a cached permutation per
column, replacing per-keystroke sorting), hover-based `href` rewriting so
middle-click and ⌘-click keep working with deferred tokens, and the two export
buttons agreed with the user (instant permalink export; source-link export that
states its download size).

**Vendor names are fragmented.** 6,742 vendor strings collapse to 1,987 real
companies — the state records the same firm many ways, often with a contract
number appended (`BOCKMANN, INC`, `BOCKMANN, INC. 14721`, `BOCKMANN INC.` …
174 spellings). The site therefore reports Bockmann's largest contract as
$4.5 M when the real total is $10.4 M. This is the highest-value unbuilt
improvement: it is what makes "how much has Nebraska paid this company?"
answerable. Do it as an inspectable mapping file, like `type_groups.json`, not
blind normalization.

**Incremental daily updates.** A full re-scrape is 20+ hours and can't run
nightly, but Active contracts are only 7.3% of pages, and expirations can be
inferred from what leaves Active. Est. 30–45 min nightly. Needs a baseline
diff, which now exists.

**Full-text search** (`prototype-search/`) is live but unlinked, covering
16,467 documents. Query latency is unresolved — see the range-request finding
above for a likely cause. Do not extend it to the new corpus before that is
understood.

**"Scope of work" column** was requested by another reporter. The state exposes
no such field anywhere, so it can only come from inside the PDFs: ~47% are
scanned images needing OCR that doesn't exist yet, and turning contract text
into a one-line scope realistically needs an LLM pass over 150,000+ documents.
Feasible but the largest remaining project; a narrow single-agency pilot would
prove the summaries before committing.
