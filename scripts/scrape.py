import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import argparse
import datetime
import json
import statistics
import sys
import threading
import time
import csv
import os

BASE_URL = "https://statecontracts.nebraska.gov"
SEARCH_URL = f"{BASE_URL}/Search"
RESULTS_URL = f"{BASE_URL}/Search/SearchResults"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The site splits entities into two categories that behave differently enough
# to need separate code paths (see scrape_entity and parse_results_page):
# Higher Education searches take a DocType (Contract vs Purchase Order) and
# render into a table#entitygrid; State searches have no DocType concept at
# all and render into a table#agencygrid.
HIGHER_ED_ENTITIES = {
    "Chadron State College": "Chadron State College-050-001",
    "Nebraska State College System": "Nebraska State College System-050-000",
    "Peru State College": "Peru State College-050-003",
    "University of Nebraska Central Administration": "University of Nebraska Central Administration-051-009",
    "University of Nebraska Kearney": "University of Nebraska Kearney-051-005",
    "University of Nebraska Lincoln": "University of Nebraska Lincoln-051-002",
    "University of Nebraska Medical Center": "University of Nebraska Medical Center-051-003",
    "University of Nebraska Omaha": "University of Nebraska Omaha-051-004",
    "Wayne State College": "Wayne State College-050-004",
}

# The 83 State agencies live in their own file purely because that much pure
# data crowds out the logic here. scripts/check_entity_drift.py re-fetches
# both lists from the site and reports what has changed.
with open(os.path.join(ROOT, "scripts", "state_entities.json"), encoding="utf-8") as _f:
    STATE_ENTITIES = json.load(_f)

STATUSES = ["Active", "Expired"]

# What each CLI mode scrapes: the entity category, its DocType (None where the
# site has no such filter), and the CSV it lands in.
DATASETS = {
    "purchase-order": ("Higher Education", "Purchase Order", "data/nu_purchase_orders.csv"),
    "contract": ("Higher Education", "Contract", "data/nu_contracts.csv"),
    "state": ("State", None, "data/state_agencies.csv"),
}

# Completion times, one per dataset, so the published page can say how fresh
# its data is. Each dataset is scraped by a separate run, so this file
# accumulates a stamp per dataset rather than being overwritten wholesale.
SCRAPE_META = "data/scrape_meta.json"

# Which (entity, status) combos each dataset has finished, so a run that stops
# partway knows where to pick up. Every dataset gets this: a Higher Education
# purchase-order run was lost an hour in to a single connection timeout, which
# is exactly the failure a checkpoint exists to absorb.

# Pacing, set from measurement rather than guesswork. A page of 25 records
# costs one results request plus 25 detail requests, so detail fetches are
# ~96% of all traffic and MAX_WORKERS/DETAIL_DELAY are what actually govern
# load.
#
# MAX_WORKERS stays at 15: an A/B against the live site (same agency, same
# page) showed 15 and 20 workers finishing in the same 3.98s, but median
# detail latency rising 1.15s -> 1.42s at 20. The site is already the
# bottleneck, so extra concurrency only queues up and makes it work harder
# for nothing.
#
# PAGE_DELAY was a full second of dead sleep before each results request, but
# consecutive results requests are already separated by the ~2.6s of detail
# fetching between them, so most of that second bought nothing. Trimming it
# is the one lever here that buys real time without asking more of the site.
MAX_WORKERS = 15
DETAIL_DELAY = 0.15
PAGE_DELAY = 0.3

# Generous, because a few searches genuinely are this slow (see scrape_entity).
SEARCH_TIMEOUT = 300

# Detail-fetch latency is the early warning that we are asking too much: if the
# site starts straining, responses slow down before they start failing. Each
# combo reports its median so a run that drifts upward is visible in the log
# rather than silently hammering a struggling server.
_latency_lock = threading.Lock()
_latencies = []


def build_session():
    """Session with a connection pool sized for our workers and retries on transient failures."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; research scraper)",
        "Referer": SEARCH_URL,
    })
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=MAX_WORKERS * 2,
        pool_maxsize=MAX_WORKERS * 2,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Detail pages carry all their state in the URL, so they can be fetched on sessions
# that never ran a search. This matters a lot: once a session holds search results,
# the server serializes every request on that session's state lock, so concurrent
# detail fetches queue up behind each other (~26s per 25 vs ~5s on clean sessions).
_local = threading.local()


def detail_session():
    """A per-thread session that never runs a search, so it holds no server-side state."""
    if not hasattr(_local, "session"):
        _local.session = build_session()
    return _local.session


def get_token(session):
    resp = session.get(SEARCH_URL)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    token = soup.find("input", {"name": "__RequestVerificationToken"})
    if not token:
        raise ValueError("Could not find CSRF token")
    return token["value"]


def get_view_url(detail_url):
    """Fetch the detail page and return the View URL.

    Three outcomes, deliberately distinct -- see README.md, "Things that bit
    us". Returning "" for both of the last two is what made the 17 Aug 2026
    outage dangerous: the state stopped offering documents, and a blank would
    have been written as though those contracts simply had no file attached.

      a URL -- the page offers a document
      ""    -- the page was read cleanly and offers no document (a real fact)
      None  -- we could not find out (fetch failed); the caller must not
               record this as "no document"
    """
    if not detail_url:
        return ""
    try:
        time.sleep(DETAIL_DELAY)
        started = time.time()
        resp = detail_session().get(detail_url, timeout=30)
        with _latency_lock:
            _latencies.append(time.time() - started)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        view_link = soup.find("a", href=lambda h: h and "ViewDocument" in h)
        return BASE_URL + view_link["href"] if view_link else ""
    except Exception as e:
        print(f"    Warning: could not fetch view URL: {e}")
        return None


def drain_latency():
    """Median detail-fetch seconds since the last call, or None if nothing was fetched."""
    with _latency_lock:
        if not _latencies:
            return None
        median = statistics.median(_latencies)
        _latencies.clear()
        return median


_detail_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# One worker, so at most a single results page is ever in flight. The point is
# to overlap that fetch with the detail fetches, not to add search load.
_page_pool = ThreadPoolExecutor(max_workers=1)


def _fetch_page(session, page):
    """Fetch one results page on the searching session, honouring the pacing delay."""
    time.sleep(PAGE_DELAY)
    return session.get(f"{RESULTS_URL}?page={page}", timeout=SEARCH_TIMEOUT)


def fetch_view_urls_parallel(records):
    """Fetch view URLs for a page of records in parallel, on search-free sessions."""
    return list(_detail_pool.map(lambda r: get_view_url(r["detail_url"]), records))


# Three documents confirmed to carry a file. They are the only way to tell the
# state's document service being down from a contract genuinely having no
# document attached: during the 17-18 Aug 2026 outage the detail pages still
# returned HTTP 200 and simply rendered no link, which is exactly what a
# document-less contract looks like. If all three stop offering their files,
# the service is down and every "no document" result that run is meaningless.
CANARY_DOCUMENTS = {
    "80-2-1555": "https://statecontracts.nebraska.gov/Search/SearchDocuments?A=JzaHHD760b8rdX81uloiRg%3D%3D&D=GxHL1l2mAft6sKHhVz8uqw%3D%3D&DN=gAjNnsOJRLzIbjJg2AGS%2Fw%3D%3D&N=PRMfkBXqcZegg9sdKXw1Lul3Fxe2B71Qc3b2LTSP1js%3D&DT=alQT0%2B51hm2oW6NMeITLVg%3D%3D&V=Vyq9bnboIUEIEOc9KpdakDF1mS79Sy4Ct%2FObKZSljxht2MIJSSl2nn76ptH0xy1sxtgWROJy7mYt72W7e%2FeK3g%3D%3D",
    "80-2-1468": "https://statecontracts.nebraska.gov/Search/SearchDocuments?A=JzaHHD760b8rdX81uloiRg%3D%3D&D=GxHL1l2mAft6sKHhVz8uqw%3D%3D&DN=yVq7GmvYRDwOK3nz%2FBAkdQ%3D%3D&N=PRMfkBXqcZegg9sdKXw1Lul3Fxe2B71Qc3b2LTSP1js%3D&DT=alQT0%2B51hm2oW6NMeITLVg%3D%3D&V=qHt%2BZVskc%2BlCzCaF0qJkip3%2BiXkq7UwM%2FHqv05kNaoRkDeQrMN4MWRsRPonbcE1c",
    "80-2-1726": "https://statecontracts.nebraska.gov/Search/SearchDocuments?A=JzaHHD760b8rdX81uloiRg%3D%3D&D=GxHL1l2mAft6sKHhVz8uqw%3D%3D&DN=bZNGd7yFWZxPEM9k2x7PPg%3D%3D&N=PRMfkBXqcZegg9sdKXw1Lul3Fxe2B71Qc3b2LTSP1js%3D&DT=alQT0%2B51hm2oW6NMeITLVg%3D%3D&V=ZIuoaTkaf7JjmSN%2Bq%2FOwzx8KfuSYTfATMkmFHCz%2BL6o%3D",
}


def document_service_healthy():
    """Is the state still serving documents at all?

    Returns True unless every canary document reports "no document" while its
    page reads cleanly. Deliberately biased towards True: a canary we cannot
    fetch proves nothing (the URL may simply have rotted), so it is skipped
    rather than counted against the state. Only a clean page that has stopped
    offering a file we know exists is treated as evidence.
    """
    checked = 0
    for doc, detail_url in CANARY_DOCUMENTS.items():
        result = get_view_url(detail_url)
        if result is None:
            continue          # could not tell -- proves nothing either way
        checked += 1
        if result:
            return True       # one working document is enough
        print(f"    Canary {doc}: page reads clean but offers no document.")
    if checked == 0:
        print("    Warning: no canary document could be checked; assuming the "
              "state is up. If this persists the canary URLs have rotted.")
        return True
    return False


def parse_results_page(soup):
    """Parse a results page. Returns (records, last_page_number)."""
    records = []
    # Higher Education results render into #entitygrid, State results into
    # #agencygrid. Only one ever exists on a given page, so trying both can't
    # misparse -- and matching neither would silently look like "no results".
    table = soup.find("table", id="entitygrid") or soup.find("table", id="agencygrid")
    if not table:
        return records, None

    tbody = table.find("tbody")
    if not tbody:
        return records, None

    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 8:
            continue

        a_tag = cells[0].find("a")
        doc_number = a_tag.text.strip() if a_tag else cells[0].text.strip()
        detail_url = BASE_URL + a_tag["href"] if (a_tag and "href" in a_tag.attrs) else ""

        records.append({
            "doc_number": doc_number,
            "doc_type": cells[1].text.strip(),
            "entity_code": cells[2].text.strip(),
            "entity_name": cells[3].text.strip(),
            "vendor": cells[4].text.strip(),
            "amount": cells[5].text.strip(),
            "begin_date": cells[6].text.strip(),
            "end_date": cells[7].text.strip(),
            "detail_url": detail_url,
        })

    # Find the last page number from pagination links
    last_page = None
    tfoot = table.find("tfoot")
    if tfoot:
        for link in tfoot.find_all("a"):
            href = link.get("href", "")
            if "page=" in href:
                try:
                    page_num = int(href.split("page=")[-1])
                    if last_page is None or page_num > last_page:
                        last_page = page_num
                except ValueError:
                    pass

    return records, last_page


def scrape_entity(session, entity_name, entity_val, status, entity_type, doc_type, writer,
                  start_page=1, on_page=None, deadline=None,
                  record_filter=None, on_record_seen=None):
    """Scrape one (entity, status) combo. Returns (rows_written, finished).

    `finished` is False when a deadline cut the combo short; the caller records
    how far it got so a later run resumes there. Resuming costs one search --
    the site holds results in session state, so page N is only reachable by
    re-running the query -- but that is a couple of minutes against combos that
    otherwise take hours to redo.

    `record_filter` and `on_record_seen` exist for --daily mode (see run_daily):
    when given, only records passing `record_filter` get a detail/view-URL
    fetch and a CSV row, while `on_record_seen` still fires for every parsed
    record so the caller can diff what's on the page against what it already
    knew, independent of what got written. Both default to None, which
    reproduces the original "write every record" behaviour exactly.
    """
    label = f"{entity_name} | {status}" + (f" | {doc_type}" if doc_type else "")
    print(f"\nScraping: {label}" + (f"  (resuming at page {start_page:,})" if start_page > 1 else ""))

    token = get_token(session)
    data = {
        "__RequestVerificationToken": token,
        "TempEntity": entity_val,
        "Status": status,
        "Type": entity_type,
        "Entity": entity_val,
        "Vendor": "",
        "Amount": "0",
    }
    # State searches have no Contract/Purchase Order filter -- the site hides
    # that dropdown entirely for them -- so the param is omitted, not blanked.
    if doc_type is not None:
        data["DocType"] = doc_type

    time.sleep(PAGE_DELAY)
    # The initial search is by far the slowest request: the site computes the
    # whole result set before returning page 1, and the largest combos measured
    # 108-144s. A 60s limit killed those runs outright, and the retries just
    # re-ran the same slow query. Paging through the computed set is fast.
    resp = session.post(RESULTS_URL, data=data, timeout=SEARCH_TIMEOUT)
    resp.raise_for_status()

    page = 1
    if start_page > 1:
        time.sleep(PAGE_DELAY)
        resp = session.get(f"{RESULTS_URL}?page={start_page}", timeout=SEARCH_TIMEOUT)
        resp.raise_for_status()
        page = start_page

    total = 0
    deferred = 0      # records held back because their detail page could not be read
    prefetch = None   # next page's response, fetched while this page's details are in flight
    recovered = False

    def run_search():
        """Re-run the query. Results live in server-side session state, so this
        is the only way back to a given page after that state goes away."""
        time.sleep(PAGE_DELAY)
        r = session.post(RESULTS_URL, data={**data, "__RequestVerificationToken": get_token(session)},
                         timeout=SEARCH_TIMEOUT)
        r.raise_for_status()
        return r

    while True:
        soup = BeautifulSoup(resp.text, "html.parser")
        records, last_page = parse_results_page(soup)
        empty = ("No results found" in resp.text) or not records

        # An empty page partway through does not mean the results ran out --
        # the site holds them in session state that expires, and once it does
        # every further page comes back blank. Treating that as the end is how
        # a 7,139-page combo quietly finished at 5,777 and marked itself
        # complete. Re-run the search and return to where we were; only call it
        # the end if the page is still empty afterwards.
        if empty and page > start_page and not recovered:
            print(f"  Page {page:,} came back empty -- search state likely expired, re-running query.")
            resp = run_search()
            if page > 1:
                time.sleep(PAGE_DELAY)
                resp = session.get(f"{RESULTS_URL}?page={page}", timeout=SEARCH_TIMEOUT)
                resp.raise_for_status()
            recovered = True
            continue

        if empty:
            if page > start_page:
                # Still empty after a fresh search: stop, but do not claim the
                # combo is finished -- a later run resumes here and re-checks.
                print(f"  Page {page:,} still empty after re-running the search -- stopping, not marking complete.")
                return total, False
            print("  No results found.")
            break

        recovered = False

        # Start the next results page before spending ~2.5s on this page's
        # detail fetches. The two use different connections -- details run on
        # search-free sessions precisely so they don't queue behind search
        # state -- so the ~1.4s results fetch costs nothing once hidden behind
        # them. This is one extra in-flight request, not more detail workers:
        # the detail endpoint is already saturated at 15, where 20 measured the
        # same throughput with worse latency.
        more_pages = last_page is not None and page < last_page
        if more_pages:
            prefetch = _page_pool.submit(_fetch_page, session, page + 1)

        to_fetch = [r for r in records if record_filter is None or record_filter(r)]
        view_urls = fetch_view_urls_parallel(to_fetch)
        view_url_by_detail = dict(zip((r["detail_url"] for r in to_fetch), view_urls))

        for record in records:
            if on_record_seen:
                on_record_seen(record)
            if record_filter is not None and not record_filter(record):
                continue
            view_url = view_url_by_detail.get(record["detail_url"], "")
            if view_url is None:
                # We could not read this record's detail page, so we do not know
                # whether it has a document. Writing it now would record "no
                # document" as a fact. Leave it out: it stays unknown to the CSV,
                # so the next run picks it up again and asks properly.
                deferred += 1
                continue
            writer.writerow([
                record["doc_number"],
                record["doc_type"],
                record["entity_code"],
                # The canonical dropdown name, not the site's rendering of it
                # (State grids shout it in caps: "DRY BEAN COMMISSION"). Two
                # things downstream match on this exact string: the resume
                # checkpoint here, and build_site.py's ENTITIES guard, which
                # hard-exits on any name it doesn't recognize. The agency code
                # column still carries the site's own identifier.
                entity_name,
                record["vendor"],
                record["amount"],
                record["begin_date"],
                record["end_date"],
                status,
                record["detail_url"],
                view_url,
            ])

        total += len(records)
        if page % 20 == 0 or page == last_page or page == start_page:
            of = f"/{last_page:,}" if last_page else ""
            print(f"  Page {page:,}{of}: {total:,} records this run")

        # Only after the rows are on disk, so a resume never skips a page whose
        # rows were lost. The reverse -- redoing a page whose rows did land --
        # costs 25 duplicate rows, which build_site.py drops.
        if on_page:
            on_page(page)

        if not more_pages:
            break

        if deadline and time.time() >= deadline:
            print(f"  Time limit reached at page {page:,} of {last_page:,} -- stopping here.")
            if prefetch:
                prefetch.cancel()
            return total, False

        page += 1
        # Usually already done, since it ran alongside the detail fetches.
        resp = prefetch.result()
        prefetch = None
        resp.raise_for_status()

    median = drain_latency()
    if median is not None:
        # ~0.95s is the site's healthy baseline. A sustained climb means we are
        # asking for more than it wants to give, and the pacing above should
        # come back down.
        flag = "  <-- server slowing, consider easing pacing" if median > 2.0 else ""
        print(f"  Median detail fetch: {median:.2f}s{flag}")

    # Did we actually reach the last page? This is checked on the page counter
    # rather than the row count so it holds for a resumed combo too -- the
    # earlier row-count version was skipped exactly when a combo resumed, which
    # is when it was needed.
    if last_page and page < last_page:
        print(f"  NOTE: stopped at page {page:,} of {last_page:,} -- not marking complete.")
        return total, False

    # The site adds records daily, so a scrape spanning hours has rows inserted
    # underneath it and page boundaries shift. That normally shows up as a few
    # repeated rows, which build_site.py drops, but the same movement could in
    # principle skip some.
    if last_page and start_page == 1:
        expected = (last_page - 1) * 25
        if total < expected * 0.98:
            print(f"  NOTE: {total:,} rows for {last_page:,} pages, fewer than the "
                  f"~{expected:,} expected -- records may have shifted mid-scrape.")

    if deferred:
        print(f"  {entity_name}: {deferred:,} record(s) held back -- their detail pages "
              "could not be read, so whether they carry a document is still unknown. "
              "They stay unrecorded and will be retried on the next run.")
    return total, True


def record_scrape_time(dataset):
    """Stamp this dataset's completion time, preserving stamps for other datasets."""
    stamps = {}
    if os.path.exists(SCRAPE_META):
        try:
            with open(SCRAPE_META, encoding="utf-8") as f:
                stamps = json.load(f)
        except (OSError, ValueError):
            stamps = {}  # unreadable or corrupt: start fresh rather than fail the scrape

    # Local time with an explicit UTC offset, so the browser can render it in
    # the reader's own timezone without guessing where the scrape ran.
    stamps[dataset] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    with open(SCRAPE_META, "w", encoding="utf-8") as f:
        json.dump(stamps, f, indent=2, sort_keys=True)
    return stamps[dataset]


def progress_file(dataset):
    return f"data/{dataset}_scrape_progress.json"


def load_progress(path):
    """(finished combos, {unfinished combo: last page written}).

    The combos here run to thousands of pages -- the largest is about ten hours
    -- so finishing is far too coarse a unit to checkpoint on. Page position is
    tracked too, which is what lets an interrupted run pick up mid-combo rather
    than repeat a day's work.
    """
    if not os.path.exists(path):
        return set(), {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return set(), {}  # unreadable or corrupt: treat as a fresh start

    if isinstance(raw, list):  # written before page tracking existed
        return {tuple(pair) for pair in raw}, {}
    done = {tuple(pair) for pair in raw.get("done", [])}
    partial = {(e, s): page for e, s, page in raw.get("partial", [])}
    return done, partial


def save_progress(path, done, partial):
    payload = {
        "done": sorted(list(pair) for pair in done),
        "partial": sorted([e, s, page] for (e, s), page in partial.items()),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)  # atomic, so a kill mid-write can't corrupt the checkpoint


def drop_unfinished_rows(csv_path, keep):
    """Strip rows belonging to combos we have no position for.

    A combo that is finished, or that has a recorded page position, has its
    rows accounted for and keeps them. Anything else was cut off before the
    first page was checkpointed, so its rows would be re-scraped and
    duplicated -- those are dropped.
    """
    if not os.path.exists(csv_path):
        return 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return 0

    header, body = rows[0], rows[1:]
    # Entity Name is column 3, Status is column 8 -- see the header written in main().
    kept = [r for r in body if len(r) > 8 and (r[3], r[8]) in keep]
    dropped = len(body) - len(kept)

    if dropped:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(kept)
    return dropped


# Fields worth watching for change, as (record key, CSV column, label).
# Deliberately not Status: --daily already derives Active -> Expired from
# presence, patches the CSV, and counts it in the diff report.
WATCHED_FIELDS = (
    ("amount", 5, "amount"),
    ("vendor", 4, "vendor"),
    ("begin_date", 6, "begin date"),
    ("end_date", 7, "end date"),
)

CHANGES_JSONL = "data/changes.jsonl"


def load_known_active(csv_path):
    """({entity_name: {detail_url, ...}}, {detail_url: (values...)}) for Active rows.

    None (not a pair) means the CSV doesn't exist at all -- --daily's signal to
    refuse rather than treat a missing baseline as "nothing was ever active".
    Read once at the start of a daily run, before any scraping, so each
    entity's diff has a stable baseline unaffected by rows this same run
    appends or patches.

    The second half is the baseline for amendments. Until Aug 2026 a record
    that was already known was skipped outright, so a contract whose amount
    changed looked identical to one that had not moved: document 45500 at the
    Medical Center went from $2,558,983 to $21,204,743 and the database could
    not show it. Only the Active rows are carried, which is the same set
    --daily re-scans, so this costs a tuple per Active row and nothing else.
    """
    if not os.path.exists(csv_path):
        return None
    known = {}
    baseline = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for r in reader:
            # Entity Name is column 3, Status is column 8, Detail URL is column 9.
            if len(r) > 9 and r[8] == "Active":
                known.setdefault(r[3], set()).add(r[9])
                baseline[r[9]] = tuple(r[column] for _key, column, _label in WATCHED_FIELDS)
    return known, baseline


def record_changes(record, baseline, out_path=CHANGES_JSONL):
    """Append one entry per watched field that moved. Returns how many.

    Append-only with last-entry-wins, the same shape as data/doc_text.jsonl --
    so read it by folding on `key` and keeping the last entry, never by
    counting lines. Each entry stands alone: the document it describes, the
    field, both values, and the date it was observed.
    """
    was = baseline.get(record["detail_url"])
    if was is None:
        return 0

    changes = []
    for (key, _column, label), before in zip(WATCHED_FIELDS, was):
        now = record.get(key, "")
        if now != before:
            changes.append({
                "key": f"{record['detail_url']}|{label}",
                "doc": record["doc_number"],
                "type": record["doc_type"],
                "entity": record["entity_name"],
                "field": label,
                "from": before,
                "to": now,
                "seen": datetime.date.today().isoformat(),
                "detail_url": record["detail_url"],
            })
    if not changes:
        return 0

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        for change in changes:
            f.write(json.dumps(change, ensure_ascii=False) + "\n")
    return len(changes)


def patch_status_to_expired(csv_path, flip_urls):
    """Flip Status Active -> Expired for existing rows whose Detail URL is in flip_urls.

    One full rewrite, same shape as drop_unfinished_rows -- there is no index
    to seek by. Called once per dataset per --daily run, after every entity
    has been scraped, not once per entity.
    """
    if not flip_urls or not os.path.exists(csv_path):
        return 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return 0

    header, body = rows[0], rows[1:]
    patched = 0
    for r in body:
        if len(r) > 9 and r[8] == "Active" and r[9] in flip_urls:
            r[8] = "Expired"
            patched += 1

    if patched:
        tmp = csv_path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(body)
        os.replace(tmp, csv_path)  # atomic, same reasoning as save_progress
    return patched


DAILY_DIFF_REPORT = "data/daily_diff_report.json"


def append_daily_diff_report(dataset, entity_report, stamp):
    """Append one dataset-run's per-entity counts to the week's accumulating report.

    Consumed by scripts/check_daily_diff.py on the weekly publish day, then
    cleared. See that script for how the totals here get used as a guard rail.
    """
    entries = []
    if os.path.exists(DAILY_DIFF_REPORT):
        try:
            with open(DAILY_DIFF_REPORT, encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, ValueError):
            entries = []  # unreadable or corrupt: start this week's report fresh

    totals = {k: sum(e[k] for e in entity_report.values())
              for k in ("previously_active", "still_active", "newly_active", "flipped_to_expired")}
    entries.append({"dataset": dataset, "timestamp": stamp, "entities": entity_report, "totals": totals})

    tmp = DAILY_DIFF_REPORT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, sort_keys=True)
    os.replace(tmp, DAILY_DIFF_REPORT)


def daily_progress_file(dataset):
    return f"data/{dataset}_daily_progress.json"


def load_daily_progress(path):
    """Entities already finished in *today's* --daily run, or empty if this is a new day.

    Unlike the full-scrape checkpoint, this is never meant to persist across
    days -- it exists only so a same-day retry (e.g. re-running a workflow
    that hit its time limit) doesn't repeat entities already done today. A
    stored date that isn't today's is treated as stale, so every new day
    re-scans every entity's Active status from scratch, same as if this file
    didn't exist.
    """
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return set()
    if raw.get("date") != datetime.date.today().isoformat():
        return set()
    return set(raw.get("done_entities", []))


def save_daily_progress(path, done_entities):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"date": datetime.date.today().isoformat(),
                   "done_entities": sorted(done_entities)}, f, indent=2)
    os.replace(tmp, path)


def run_daily(args):
    """Scrape only Status=Active for one dataset and diff against what the
    existing CSV already knows is Active -- see README.md, "Daily updates"
    for why this is safe (build_site.py's exact-fingerprint dedup) and what
    it deliberately does not catch (amendments to an already-Active record).
    """
    entity_type, doc_type, output_csv = DATASETS[args.dataset]
    is_state = args.dataset == "state"
    entities = STATE_ENTITIES if is_state else HIGHER_ED_ENTITIES

    if args.entity:
        unknown = [e for e in args.entity if e not in entities]
        if unknown:
            known = "scripts/state_entities.json" if is_state else "HIGHER_ED_ENTITIES in scripts/scrape.py"
            print(f"unknown entity name(s) for dataset {args.dataset!r}: {unknown}. See {known}.", file=sys.stderr)
            sys.exit(2)
        entities = {name: entities[name] for name in args.entity}

    os.makedirs("data", exist_ok=True)

    loaded = load_known_active(output_csv)
    known_active, baseline = loaded if loaded else (None, None)
    if known_active is None:
        print(f"ERROR: refusing --daily: {output_csv} does not exist. This dataset needs a "
              "completed full scrape (or a restored data/ cache) before daily mode has anything "
              "to diff against.", file=sys.stderr)
        sys.exit(1)

    # Check the state is still serving documents before scraping a single page.
    # A run made while its document service is down records "no document" for
    # every new contract, which is indistinguishable afterwards from the truth
    # and would ship to the site as missing links. One request answers this;
    # the alternative is thousands of requests producing poisoned rows.
    if not document_service_healthy():
        print("ERROR: refusing --daily: the state is not serving documents right now, so "
              "every new record would be written as having none. Nothing was scraped. "
              "Re-run once https://statecontracts.nebraska.gov is serving documents again.",
              file=sys.stderr)
        sys.exit(1)

    progress_path = daily_progress_file(args.dataset)
    done_today = load_daily_progress(progress_path)

    write_header = not os.path.exists(output_csv) or os.path.getsize(output_csv) == 0
    start = time.time()
    deadline = start + args.hours * 3600 if args.hours else None

    flips = {}    # entity_name -> set(detail_url) flipped to Expired
    report = {}   # entity_name -> counts dict
    amended = 0   # watched fields seen to have moved, written to changes.jsonl

    with open(output_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_HEADER)

        session = build_session()
        for entity_name in ordered_entities(entities):
            if entity_name in done_today:
                continue
            if deadline and time.time() >= deadline:
                print(f"\n{args.hours}h time limit reached -- stopping cleanly.")
                break

            entity_val = entities[entity_name]
            known = known_active.get(entity_name, set())
            seen = set()

            def note_change(record):
                nonlocal amended
                amended += record_changes(record, baseline)

            count, finished = scrape_entity(
                session, entity_name, entity_val, "Active", entity_type, doc_type, writer,
                deadline=deadline,
                record_filter=lambda r, _k=known: r["detail_url"] not in _k,
                on_record_seen=lambda r, _s=seen: (_s.add(r["detail_url"]),
                                                   note_change(r)),
            )
            f.flush()

            if not finished:
                # Deadline or a recovery failure -- don't mark done, don't compute
                # flips off an incomplete "seen" set. It retries from page 1 next
                # run (today's retry, or tomorrow's fresh run).
                print(f"  {entity_name}: not finished this run, will re-scan from the top later.")
                continue

            report[entity_name] = {
                "previously_active": len(known),
                "still_active": len(seen & known),
                "newly_active": len(seen - known),
                "flipped_to_expired": len(known - seen),
            }
            flips[entity_name] = known - seen

            done_today.add(entity_name)
            save_daily_progress(progress_path, done_today)

    if amended:
        print(f"Recorded {amended:,} field change(s) to {CHANGES_JSONL}.")

    all_flip_urls = {u for s in flips.values() for u in s}
    if all_flip_urls:
        patched = patch_status_to_expired(output_csv, all_flip_urls)
        print(f"Patched {patched:,} row(s) Active -> Expired.")

    if all(e in done_today for e in entities):
        stamp = record_scrape_time(args.dataset)
        append_daily_diff_report(args.dataset, report, stamp)
        if os.path.exists(progress_path):
            os.remove(progress_path)  # today is done; tomorrow starts fresh anyway, but tidy
        print(f"\nDaily scrape complete for {args.dataset}. Scrape time recorded: {stamp}")
    else:
        remaining = sorted(set(entities) - done_today)
        print(f"\nDaily scrape incomplete -- {len(remaining)} entit(y/ies) not finished today: "
              f"{remaining[:5]}{'...' if len(remaining) > 5 else ''}. Re-run to finish before "
              "the report is finalized.")


CSV_HEADER = [
    "Document Number", "Document Type", "Entity Code", "Entity Name",
    "Vendor", "Amount", "Begin Date", "End Date", "Status", "Detail URL", "View URL"
]

# Scraped first so an interrupted run still lands the highest-value data. The
# rest follow in whatever order the entity list gives.
STATE_PRIORITY = [
    "Health & Human Services, Department of",
    "Correctional Services, Department of",
    "Roads, Department of",
    "Administrative Services, Department of",
]


def ordered_entities(entities):
    """Entity names in scrape order, biggest known agencies first (STATE_PRIORITY)."""
    return sorted(entities, key=lambda n: (STATE_PRIORITY.index(n) if n in STATE_PRIORITY else len(STATE_PRIORITY), n))


def combos(entities):
    """(entity_name, entity_val, status) to scrape, biggest known agencies first.

    Ordering only decides what has landed if a run stops early -- the checkpoint
    makes the end result identical either way.
    """
    ordered = ordered_entities(entities)
    return [(name, entities[name], status) for name in ordered for status in STATUSES]


def main():
    parser = argparse.ArgumentParser(description="Scrape documents from the Nebraska State Contracts Database.")
    parser.add_argument("dataset", nargs="?", default="purchase-order", choices=sorted(DATASETS),
                        help="Which dataset to scrape (default: purchase-order)")
    parser.add_argument("--entity", action="append", default=None,
                        help="Only scrape these entities, by display name (repeatable).")
    parser.add_argument("--hours", type=float, default=None,
                        help="Stop cleanly after this many hours, checkpointing as normal.")
    parser.add_argument("--status", action="store_true",
                        help="Print progress counts and exit -- no network calls.")
    parser.add_argument("--fresh", action="store_true",
                        help="Discard any saved progress and re-scrape this dataset from scratch.")
    parser.add_argument("--daily", action="store_true",
                        help="Scrape only Status=Active and diff against the existing CSV, "
                             "instead of a full (entity, status) sweep.")
    args = parser.parse_args()

    if args.daily:
        if args.fresh or args.status:
            parser.error("--daily is incompatible with --fresh/--status (it always reads and "
                          "patches the existing CSV in place; it never starts fresh).")
        run_daily(args)
        return

    entity_type, doc_type, output_csv = DATASETS[args.dataset]
    is_state = args.dataset == "state"

    entities = STATE_ENTITIES if is_state else HIGHER_ED_ENTITIES

    if args.entity:
        unknown = [e for e in args.entity if e not in entities]
        if unknown:
            known = "scripts/state_entities.json" if is_state else "HIGHER_ED_ENTITIES in scripts/scrape.py"
            parser.error(f"unknown entity name(s) for dataset {args.dataset!r}: {unknown}. See {known}.")
        entities = {name: entities[name] for name in args.entity}

    os.makedirs("data", exist_ok=True)

    # Same guard as --daily, and it matters more here: a full sweep is 20+ hours,
    # so an outage that starts mid-run would silently mark every remaining
    # contract as document-less. --status takes no network calls and skips it.
    if not args.status and not document_service_healthy():
        print("ERROR: the state is not serving documents right now, so every record "
              "scraped would be written as having none. Nothing was scraped. Re-run "
              "once https://statecontracts.nebraska.gov is serving documents again.",
              file=sys.stderr)
        sys.exit(1)

    progress_path = progress_file(args.dataset)

    if args.fresh:
        for p in (progress_path, output_csv):
            if os.path.exists(p):
                os.remove(p)
        print(f"--fresh: cleared {progress_path} and {output_csv}")

    done, partial = load_progress(progress_path)
    all_combos = combos(entities)
    todo = [c for c in all_combos if (c[0], c[2]) not in done]

    if args.status:
        print(f"TOTAL={len(all_combos)}")
        print(f"DONE={len(all_combos) - len(todo)}")
        print(f"REMAINING={len(todo)}")
        for (e, s), page in sorted(partial.items()):
            print(f"PARTIAL={e} | {s} | through page {page}")
        return

    print(f"{len(todo):,} (entity, status) combos remaining of {len(all_combos):,}")

    dropped = drop_unfinished_rows(output_csv, done | set(partial))
    if dropped:
        print(f"Discarded {dropped:,} rows from a combo cut off before its first checkpoint.")

    write_header = not os.path.exists(output_csv) or os.path.getsize(output_csv) == 0
    start = time.time()
    deadline = start + args.hours * 3600 if args.hours else None

    with open(output_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_HEADER)

        session = build_session()
        grand_total = 0

        for entity_name, entity_val, status in todo:
            if deadline and time.time() >= deadline:
                print(f"\n{args.hours}h time limit reached -- stopping cleanly.")
                break

            key = (entity_name, status)

            def checkpoint(page, _key=key):
                f.flush()
                partial[_key] = page
                save_progress(progress_path, done, partial)

            count, finished = scrape_entity(
                session, entity_name, entity_val, status, entity_type, doc_type, writer,
                start_page=partial.get(key, 0) + 1, on_page=checkpoint, deadline=deadline,
            )
            grand_total += count
            f.flush()

            if finished:
                done.add(key)
                partial.pop(key, None)
            save_progress(progress_path, done, partial)

    remaining = len([c for c in all_combos if (c[0], c[2]) not in done])
    if remaining:
        print(f"\nStopped with {remaining:,} combos remaining. Re-run to resume.")
    else:
        stamp = record_scrape_time(args.dataset)
        print(f"\nAll combos complete. Scrape time recorded: {stamp}")

    print(f"Records this run: {grand_total:,}")
    print(f"Saved to: {output_csv}")


if __name__ == "__main__":
    main()
