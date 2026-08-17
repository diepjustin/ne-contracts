# Nebraska State Contracts

**Live: https://diepjustin.github.io/ne-contracts/**

A journalism and accountability tool for scraping and publishing Nebraska state spending
records from the [Nebraska State Contracts Database](https://statecontracts.nebraska.gov/Search),
the public database mandated by **Neb. Rev. Stat. § 84-602.04**.

This folder contains two things: a scraper, and a static searchable website built from
what it collects. `index.html` lives at the folder root, alongside the `d/<buildId>/`
directory it reads, because that root is the published URL.

## Coverage

**738,195 records across 92 entities** — every state agency, board and commission the
database lists (83), plus all nine University of Nebraska and Nebraska State College
campuses. Both Active and Expired documents, contracts and purchase orders.
**Collection is complete**; the page carries no outstanding-entity warning.

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

Two campuses now hold 61% of everything, almost all of it purchase orders. On the
agency side three dominate: Correctional Services (61,783), Health & Human Services
(60,682) and Roads (39,253) are together about 70% of state agency records.

The page still names any outstanding entity itself, reading the scrapers' checkpoints,
so a partly-collected entity is never silently indistinguishable from an empty one.

## The scraper

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python3 scripts/scrape.py contract          # -> data/nu_contracts.csv
python3 scripts/scrape.py purchase-order    # -> data/nu_purchase_orders.csv
python3 scripts/scrape.py state             # -> data/state_agencies.csv
```

The site splits entities into two categories that need different handling. Higher
Education searches take a Contract/Purchase Order filter and render into a
`table#entitygrid`; State searches have no such filter and render into a
`table#agencygrid`. `scrape.py` branches on both.

The state run covers 83 agencies over many hours, so it checkpoints each completed
`(entity, status)` combo to `data/state_scrape_progress.json` and is safe to interrupt.
On resume it discards rows from any combo that was cut off mid-scrape, then continues.

```bash
python3 scripts/scrape.py state --status                    # progress, no network calls
python3 scripts/scrape.py state --hours 3                   # stop cleanly after 3 hours
python3 scripts/scrape.py state --entity "Roads, Department of"   # one agency (any dataset)
scripts/run_state_scrape_cycles.sh 3 30                     # 3h work / 30min pause, repeating
```

`scripts/check_entity_drift.py` re-fetches both entity lists from the site and reports
anything added, removed, or renamed. Run it before a full re-scrape — `build_site.py`
hard-fails on any entity name in the CSVs that isn't in its list.

Output columns: `Document Number`, `Document Type`, `Entity Code`, `Entity Name`,
`Vendor`, `Amount`, `Begin Date`, `End Date`, `Status`, `Detail URL`, `View URL`.

`Entity Name` holds the canonical dropdown name rather than the site's rendering of it
(State result grids shout it in caps: `DRY BEAN COMMISSION`). Both the resume checkpoint
and `build_site.py`'s entity guard match on that exact string. `Entity Code` still carries
the state's own identifier.

Each run stamps its completion time into `data/scrape_meta.json`, keyed by dataset, which
is where the "Last updated" line on the page comes from. `build_site.py` publishes the
**oldest** of the stamps — the dataset is only as current as its stalest part.

`View URL` links straight to the scanned document. It is blank for 6.37% of records
(47,050 of 738,195), where the state has not uploaded a file ("Documents not available
for immediate viewing"). `Detail URL` is present on every row.

A long paginated scrape needs one more thing to be correct. The site keeps its results
in server-side state that expires: after roughly 2,000 pages of continuous paging every
further page comes back "No results found", which is indistinguishable from the end of
the data. Reading it as the end once marked UNL purchase orders complete at 144,425 of
178,573 records, with no error anywhere. The scraper now re-runs the query and returns
to its position; that fired twice during the Medical Center's 9,186 pages and saved
about 170,000 records.

### A note on speed

Detail pages are fetched on a pool of sessions that have never run a search. This is
not incidental: the site keeps search results in server-side session state and
serializes every request that touches it, so concurrent fetches on the searching
session queue behind one another. Using search-free sessions took a page of 25 detail
fetches from ~26s to ~5s — about a 7x speedup over the full job. See `detail_session()`
in `scripts/scrape.py` before changing it.

Requests are still paced and retried with backoff.

Pacing is set from measurement, not intuition. A page of 25 records costs one results
request plus 25 detail requests, so detail fetches are ~96% of all traffic and
`MAX_WORKERS`/`DETAIL_DELAY` are what actually govern load. Two things worth knowing
before tuning them:

- **More workers does not mean faster.** An A/B against the live site (same agency, same
  page) had 15 and 20 workers finishing in the same ~3.98s, while median detail latency
  rose from 1.15s to 1.42s. The site is already the bottleneck; extra concurrency only
  queues up and makes it work harder for nothing.
- **`PAGE_DELAY` was mostly dead time.** Consecutive results requests are already
  separated by the ~2.6s of detail fetching between them, so a full second of additional
  sleep bought little. It is now 0.3s.

Each combo reports its median detail-fetch time, and flags a sustained climb past 2.0s.
The site's healthy baseline is ~0.95s; a run that drifts well above that is asking for
more than the site wants to give, and the pacing above should come back down.

## Daily updates

The full scrape above is a one-time (or occasional-maintenance) job. Day to day, the
state adds records under **`Status=Active`**, which is only ~7.3% of all pages, so
`scripts/scrape.py <dataset> --daily` re-scans just that status per entity and diffs it
against what the existing CSV already knows is Active, matched on `Detail URL`:

```bash
python3 scripts/scrape.py contract --daily
python3 scripts/scrape.py purchase-order --daily
python3 scripts/scrape.py state --daily
```

A record it hasn't seen before gets a detail fetch and a new row, same as a full scrape.
A record it already knew about is skipped entirely — no detail fetch, no rewrite. A
record that was previously Active but doesn't show up today gets its existing row's
`Status` flipped to `Expired` in place. This deliberately does **not** catch an amendment
to an existing Active record's Amount/Vendor/End Date — only a full re-scrape does that.

Each run appends per-entity counts to `data/daily_diff_report.json`
(gitignored, like the other progress files). `scripts/check_daily_diff.py` reads it and
fails if any single entity had more than half its previously-Active records flip to
Expired in one day — implausible as real-world attrition, but exactly what a
renamed/retired entity looks like (see `check_entity_drift.py`).

`.github/workflows/ne-contracts-daily.yml` runs `--daily` for all three datasets every
night, and on Sundays additionally runs `check_entity_drift.py` → `build_site.py` →
`check_daily_diff.py` → commit + push, in that order, so a bad week never reaches the
live site. See `HANDOFF.md` for the cache-bootstrap step this needs on a fresh clone.

## The website

`index.html` is a self-contained static site — no build step, no dependencies, no server.

```bash
python3 scripts/build_site.py   # data/*.csv -> d/<buildId>/
python3 scripts/serve_site.py   # preview at http://127.0.0.1:8765
```

`build_site.py` normalizes 362 MB of CSV into a payload split by how the page actually
uses it. It gets there first by exploiting the structure of the state's URLs: `DT` is
determined by document type, `V` maps 1:1 with vendor, and `A`/`D`/`N` vary together
across only 98 distinct combinations, so each row carries a small index instead of three
long tokens. Every URL is round-trip verified against the original before the payload is
written — 1,429,340 of them — so the compression is lossless.

That verification is worth keeping honest about: `A`, `D` and `N` each looked
entity-determined at two-entity scale, and still did across five. At 92 entities the
rule breaks — twelve agencies carry more than one `N`, and three `N` values span
agencies. The round-trip check is what caught it rather than shipping broken links.

### Why the payload is columns and not JSON

One JSON file of all 738,195 rows is ~104 MB, ~49 MB gzipped, and roughly 380 MB of
heap — minutes to load and an out-of-memory crash on a phone. The same values packed
column-wise compress about 5x better, because a column of integers is far more
compressible than the same integers interleaved with JSON punctuation.

So the payload is split by access pattern:

| | gzipped, as Pages serves it |
| --- | ---: |
| numeric columns, vendor names, document numbers — **loaded up front** | **6.94 MB** |
| link tokens — **fetched on demand**, in 361 blocks of 64 KB | 25.45 MB |

The resident half is *smaller than the 16 MB the site used to ship for 40% of the data.*
Link tokens are needed only for rows someone actually clicks or exports, so they wait.

Two findings shaped this, both verified against the live CDN rather than assumed:

- **Ranged HTTP requests are unusable on GitHub Pages.** A range request that advertises
  gzip — which browsers always do, and `fetch()` cannot override — is served against the
  *compressed* representation. Ask for bytes 100–115 and you get 16 bytes of a gzip
  stream, plus a `Content-Range` denominator that is the compressed length. Deferred data
  is therefore separate block files, never byte ranges. This probably also explains the
  unresolved query latency in the retired `prototype-search/` prototype.
- **Pages does gzip `application/octet-stream`**, so raw binary compresses in transit with
  no client-side work.

`scripts/ne_format.py` owns the layout and is the only place that knows it. Each file
holds one item size, so a section's offset is `n * itemsize * index` — no header, no
offset table, no padding, and the reader checks every file's length against `meta.count`.

Client-side cost, measured in-browser on all 738,195 rows: a keystroke filters in
**4–21 ms**, a column's first sort takes 56–199 ms and is cached after, and peak heap is
**~90 MB** — against ~154 MB for the old payload at 40% of the size. Sorting happens once
per column rather than once per keystroke: the page caches an ordering and filtering
walks it, so results come out already sorted.

Two things that only appear at this scale, both handled:

- Row ids move on every rebuild, so the shareable `?doc=` link carries document number,
  agency and type code instead. Document number alone is not unique — 14,633 of them are
  reused across agencies, covering 29,321 rows.
- Browsers cap element height at 2^24 px. All 738,195 rows at 33 px is 24.4 million, so
  the virtual scroller's spacer rows would be truncated and the last third of the table
  unreachable. Past 15 M px the scroll position is scaled onto the list instead.

Loading `/?selftest=1` checks every column against a CRC recorded at build time and
rebuilds 1,000 sampled URLs against addresses taken from the source CSVs. Run it against
the deployed site, not a local server: only a real deployment exercises gzip in transit,
and CDN behavior produced the one design-changing surprise here.

Publishing needs no configuration: this folder lives in the `diepjustin.github.io`
user site, which already serves `main` at the repo root, so pushing updates the live
page. To refresh the data, re-run the scraper, re-run `build_site.py`, then commit the
new `d/<buildId>/`, `manifest.json` and `data/scrape_meta.json`. `index.html` carries no
build identity — it reads `manifest.json` with `cache: 'no-store'` — so a reader holding
a stale copy of the page can never pair it with a different build's data.

## Data caveats

The scraper reproduces the state's records faithfully, including their errors.

- **This is not a complete record of state spending, and the gaps are not random.** The
  state's own [FAQ](https://das.nebraska.gov/materiel/contract-database/faq.html) says the
  database excludes contracts from Health & Human Services, the University of Nebraska,
  the State Colleges, Veterans' Affairs, Education, the Commission for the Blind and
  Visually Impaired, and the Nebraska Investment Finance Authority "that provide specific
  aid, assistance, or services to a specific individual." Those are among the largest
  agencies here, so their totals understate what they actually spend. Do not read an
  agency's total as its budget.
- **Purchase orders are not a Higher Education thing, even though the site makes them
  look like one.** The search form only offers a Contract/Purchase Order filter for
  Higher Education, so on the agency side the distinction is invisible — but it is in the
  records. Ten of the 32 agency document-type codes (`O9`, `OM`, `OP`, `X7`, `Y6`, `Y7`,
  `Z8`, `Z9`, `ZO`, `ZP`) are filed under "Purchase Orders", covering 152,210 records, or
  about 65% of all agency data. Overall the dataset is 619,601 purchase orders to 118,594
  contracts — the reverse of what the interface implies.
- **Document types are the source system's internal codes**, not labels — `OP`, `O4`,
  `Z4`, `ZP` and 30 others. The state publishes no key. `scripts/type_groups.json` maps
  each to the category its detail page files it under, which is what the page filters on;
  the raw code stays in the table (hover the Type cell) and in the CSV export. That
  mapping is a sampled observation, not a published key — and one code contradicts it:
  `PO`, which the site's own Purchase Order search returns, has a detail page headed
  "Contracts". The map overrides it, since the search filter is the better authority.
- **Only documents active on or after January 1, 2014 are in the database at all.**
  Anything that expired before then was never loaded, so early years are sparse in a way
  that reflects the database's construction rather than state spending.

- **A handful of records carry billion-dollar values and will dominate any total you
  compute.** The largest is `95601`, Health & Human Services / "CREIGHTON UNIVERSITY -
  ALL PAYMENTS", at $38,025,000,000.00, followed by three Medicaid managed-care
  contracts at $6,650,000,000.00 each and `58-1-1451` (EBSCO, UNL) at $4,000,000,000.00.
  Some of these are plausibly real not-to-exceed ceilings on multi-year statewide
  programs rather than errors — the state's FAQ says service contracts are valued at the
  estimated cost of the whole contract including renewals. Either way, treat the top of
  the amount column as ceilings and aggregates, not as money spent.
- **Document `41780` has a begin date of 09/28/2223** and an end date of 09/28/2023 —
  the start year should presumably be 2023. Filter on begin > end to catch this class
  of error.
- Open-ended records commonly carry an end date of `12/31/2099` or `01/01/2099`.
- Amounts are as recorded by the state and may not reflect amendments.

## Descriptions

The state publishes no "scope of work" field, so there is nothing to scrape for it. But
most documents already contain one, written by whoever filed them, and
`scripts/extract_scope.py` lifts it out of the PDF **verbatim**. Nothing is generated,
summarised or rewritten — every description on the site is the state's own words, which
is what makes it quotable. Typos, inconsistent capitalisation and abbreviations are
theirs and are kept.

They come from three places, in this order of preference:

1. **The contract cover sheet's summary field** — a sentence a person wrote. Preferred
   wherever it exists, because it beats a list of part numbers.
2. **University purchase-order line items** — a 40-character-wide description column.
3. **State agency purchase-order line items** — no fixed width.

Things worth knowing before quoting one:

- **A description is one document's text, not the contract's official scope.** It is
  whatever the filer typed. Read the source document before quoting; every row links to
  it.
- **Descriptions carry administrative text alongside the substance** — invoicing
  instructions, project reference numbers, contact names, change-order logs. These are
  kept deliberately rather than filtered, because on some documents the actual scope
  appears *after* the boilerplate, so a filter aimed at the noise removes the substance.
- **Both purchase-order forms wrap their description column across several lines**, and
  until 16 Aug 2026 both parsers read only the first. On the University form that
  truncated 93% of items — one $15 M construction PO read "GENERAL CONSTRUCTION SERVICES
  FOR" and stopped. It was found by a reader checking a document against its source, not
  by us. If you are checking this project's fidelity, that is the method that works.
- **Coverage is uneven and a blank is not a statement about the contract.** An empty
  description means the PDF could not be read — usually a scan with no text layer, or a
  document the state's own viewer does not serve — not that the contract has no scope.
  The page says so where it happens rather than leaving a blank cell to be misread.
- **Searching descriptions is opt-in.** The index is a separate download, fetched only
  when you tick the box, so it costs nothing for readers who do not use it.

## License

MIT for the code. The underlying records are public data from the Nebraska Department
of Administrative Services.
