# Nebraska University Contracts

**Live: https://diepjustin.github.io/ne-contracts/**

A journalism and accountability tool for scraping and publishing University of Nebraska
spending records from the [Nebraska State Contracts Database](https://statecontracts.nebraska.gov/Search),
the public database mandated by **Neb. Rev. Stat. § 84-602.04**.

This folder contains two things: a scraper, and a static searchable website built from
what it collects. `index.html` and `data.json` live at the folder root because that root
is the published URL.

## Coverage

| Entity | Contracts | Purchase Orders |
| --- | ---: | ---: |
| University of Nebraska Lincoln | 23,924 | 7,664 |
| University of Nebraska Central Administration | 821 | 0 |

Both Active and Expired documents. Central Administration has no purchase orders in the
state's database. Records run from 1977 to present; contracts expiring before
January 1, 2014 are not in the source database at all.

## The scraper

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python3 scripts/scrape.py contract          # -> data/nu_contracts.csv
python3 scripts/scrape.py purchase-order    # -> data/nu_purchase_orders.csv
```

Output columns: `Document Number`, `Document Type`, `Entity Code`, `Entity Name`,
`Vendor`, `Amount`, `Begin Date`, `End Date`, `Status`, `Detail URL`, `View URL`.

Each run also stamps its completion time into `data/scrape_meta.json`, keyed by document
type, which is where the "Last updated" line on the page comes from. Since the two types
are scraped separately, `build_site.py` publishes the **older** of the two stamps — the
dataset is only as current as its stalest half.

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

Requests are still paced and retried with backoff. A full run of all four
contract batches (992 pages) takes roughly an hour.

## The website

`index.html` is a self-contained static site — no build step, no dependencies, no server.

```bash
python3 scripts/build_site.py   # data/*.csv -> data.json
python3 scripts/serve_site.py   # preview at http://127.0.0.1:8765
```

`build_site.py` normalizes 15.6 MB of CSV into a 4.6 MB JSON payload (2.2 MB gzipped,
which is what GitHub Pages actually serves). It gets there by exploiting the structure
of the state's URLs: four of the six detail-URL query parameters are constant or
determined by entity/document type, and the `V` parameter maps 1:1 with vendor. Every
URL is round-trip verified against the original before the payload is written, so the
compression is lossless.

Publishing needs no configuration: this folder lives in the `diepjustin.github.io`
user site, which already serves `main` at the repo root, so pushing updates the live
page. To refresh the data, re-run the scraper, re-run `build_site.py`, then commit
`data.json` and `data/scrape_meta.json`.

## Data caveats

The scraper reproduces the state's records faithfully, including their errors.

- **Document `58-1-1451` (EBSCO) is recorded at $4,000,000,000.00.** Almost certainly a
  data-entry error in the source. It is nearly half the sum of every record in the
  database, so any aggregate you compute will be dominated by it.
- **Document `41780` has a begin date of 09/28/2223** and an end date of 09/28/2023 —
  the start year should presumably be 2023. Filter on begin > end to catch this class
  of error.
- Open-ended records commonly carry an end date of `12/31/2099` or `01/01/2099`.
- Amounts are as recorded by the state and may not reflect amendments.

## License

MIT for the code. The underlying records are public data from the Nebraska Department
of Administrative Services.
