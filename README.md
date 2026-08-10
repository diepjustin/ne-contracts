# Nebraska State Contracts

**Live: https://diepjustin.github.io/ne-contracts/**

A journalism and accountability tool for scraping and publishing Nebraska state spending
records from the [Nebraska State Contracts Database](https://statecontracts.nebraska.gov/Search),
the public database mandated by **Neb. Rev. Stat. § 84-602.04**.

This folder contains two things: a scraper, and a static searchable website built from
what it collects. `index.html` and `data.json` live at the folder root because that root
is the published URL.

## Coverage

**295,895 records across 92 entities** — every state agency, board and commission the
database lists (83), plus all nine University of Nebraska and Nebraska State College
campuses. Both Active and Expired documents.

| | Records |
| --- | ---: |
| 70 state agencies with records (13 have none) | 235,169 |
| University of Nebraska Lincoln | 31,079 |
| University of Nebraska Kearney | 18,696 |
| University of Nebraska Medical Center | 5,382 |
| Wayne State College | 2,151 |
| University of Nebraska Omaha | 1,106 |
| Chadron State College | 871 |
| University of Nebraska Central Administration | 821 |
| Peru State College | 489 |
| Nebraska State College System | 131 |

Three agencies dominate: Correctional Services (61,783), Health & Human Services
(60,682) and Roads (39,253) are together about 70% of all state agency records.

### Purchase orders are not finished yet

Contracts are complete for all nine campuses, and state agency records are complete for
all 83. **Higher Education purchase orders are still being collected** — done for
Chadron, Kearney, Peru, Central Administration and the State College System, and still
outstanding for UNL (Expired), the Medical Center, Omaha and Wayne State.

That backlog is far larger than it looks: probing the remaining searches found roughly
442,000 purchase orders outstanding, including ~229,550 for the Medical Center and
~178,500 for UNL alone. For comparison, the previous version of this dataset recorded
50 expired UNL purchase orders in total. Collecting the rest is a separate job — at
~740,000 records the single-payload approach below stops being viable and needs
splitting first.

The page names the outstanding entities itself, reading the scrapers' checkpoints, so a
partly-collected entity is never silently indistinguishable from an empty one — and the
warning disappears on its own once collection finishes.

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

`View URL` links straight to the scanned document. It is blank for 4.2% of records
(1,367 of 32,409), where the state has not uploaded a file ("Documents not available
for immediate viewing").

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

## The website

`index.html` is a self-contained static site — no build step, no dependencies, no server.

```bash
python3 scripts/build_site.py   # data/*.csv -> data.json
python3 scripts/serve_site.py   # preview at http://127.0.0.1:8765
```

`build_site.py` normalizes 144 MB of CSV into a 35 MB JSON payload (16 MB gzipped, which
is what GitHub Pages actually serves — 11% of source). It gets there by exploiting the
structure of the state's URLs: `DT` is determined by document type, `V` maps 1:1 with
vendor, and `A`/`D`/`N` vary together across only 98 distinct combinations, so each row
carries a small index instead of three long tokens. Every URL is round-trip verified
against the original before the payload is written — 548,516 of them on the current
dataset — so the compression is lossless.

That verification is worth keeping honest about: `A`, `D` and `N` each looked
entity-determined at two-entity scale, and still did across five. At 92 entities the
rule breaks — twelve agencies carry more than one `N`, and three `N` values span
agencies. The round-trip check is what caught it rather than shipping broken links.

Client-side cost of the payload, measured in-browser: 0.11s to fetch, 0.63s to parse,
0.15s to filter and sort all 295,895 rows, ~154 MB heap. The table renders only the
visible slice, so row count barely affects scrolling.

Publishing needs no configuration: this folder lives in the `diepjustin.github.io`
user site, which already serves `main` at the repo root, so pushing updates the live
page. To refresh the data, re-run the scraper, re-run `build_site.py`, then commit
`data.json` and `data/scrape_meta.json`.

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
  about 65% of all agency data. Overall the dataset is 177,301 purchase orders to 118,594
  contracts.
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

## License

MIT for the code. The underlying records are public data from the Nebraska Department
of Administrative Services.
