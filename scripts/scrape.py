import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import argparse
import datetime
import hashlib
import json
import statistics
import sys
import threading
import time
import csv
import os
import re
import shutil
from urllib.parse import parse_qs, urlsplit

BASE_URL = "https://statecontracts.nebraska.gov"
SEARCH_URL = f"{BASE_URL}/Search"
RESULTS_URL = f"{BASE_URL}/Search/SearchResults"

# Who is asking. Every outbound request in this repo sends this string, and it
# is defined here because scrape.py is the module the others already import
# from -- the same reason document_service_healthy() is imported rather than
# copied. There is one to keep current.
#
# It used to read "Mozilla/5.0 (compatible; research scraper)", which is
# anonymous and, in the Mozilla part, not true. A full run is 20+ hours against
# a state government server; when that traffic gets noticed, an administrator
# has two options -- find the operator or block the client -- and the first one
# should not require guesswork. The URL is a public page saying what this is,
# who runs it and where the code is, which is enough to be reached through
# without putting a personal address in anyone's logs.
USER_AGENT = "ne-contracts/1.0 (+https://diepjustin.github.io/ne-contracts/)"

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

# The search form is a plain page, not a computed result set, and answers in
# well under a second. requests has no default timeout, so without one a
# half-open connection here waits forever -- and get_token runs before every
# search and every re-search, so it is the one place a hung server could hold
# the whole job rather than a single request.
TOKEN_TIMEOUT = 60

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
        "User-Agent": USER_AGENT,
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
    resp = session.get(SEARCH_URL, timeout=TOKEN_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    token = soup.find("input", {"name": "__RequestVerificationToken"})
    if not token:
        raise ValueError("Could not find CSRF token")
    return token["value"]


def parse_documents(html):
    """Every document a detail page offers, in the order the state lists them.

    Returns a list of {"name", "size", "token"} -- the state's own file name
    (DOC2061661286), its own size string ("2Mb", "923Kb"), and the ViewDocument
    D token. An empty list means the page offered nothing.

    Read out of the "File Name | File Size" table rather than by scanning the
    page for ViewDocument hrefs. The name and size come free that way, and they
    are the state's own values, so a reader can check our list against the
    source. A bare href scan would also go on quietly returning a number after
    a page redesign, where a row that stops looking like a file row is
    something we want to notice.

    Each document contributes two ViewDocument links -- the file name and the
    "View" beside it, same D token -- plus a DownloadDocument link to the same
    file. Dedupe on the token, keeping first appearance, so the list is
    documents rather than links.
    """
    soup = BeautifulSoup(html, "html.parser")

    documents = []
    seen = set()
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue                     # header row: it carries <th>, not <td>
        link = cells[0].find("a", href=lambda h: h and "ViewDocument" in h)
        if not link:
            continue
        href = link["href"]
        token = href.split("D=", 1)[1] if "D=" in href else ""
        if not token or token in seen:
            continue
        seen.add(token)
        documents.append({
            "name": cells[0].get_text(" ", strip=True),
            "size": cells[1].get_text(" ", strip=True),
            "token": token,
        })
    return documents


def get_documents(detail_url):
    """Fetch a record's detail page and return every document it offers.

    Three outcomes, deliberately distinct -- see README.md, "Things that bit
    us". Collapsing the last two is what made the 17 Aug 2026 outage dangerous:
    the state stopped offering documents, and an empty answer would have been
    written as though those records simply had no file attached.

      [{...}, ...] -- the page offers these documents, in the state's order
      []           -- the page was read cleanly and offers none (a real fact)
      None         -- we could not find out (fetch failed); the caller must not
                      record this as "no document"
    """
    if not detail_url:
        return []
    try:
        time.sleep(DETAIL_DELAY)
        started = time.time()
        resp = detail_session().get(detail_url, timeout=30)
        with _latency_lock:
            _latencies.append(time.time() - started)
        resp.raise_for_status()
        return parse_documents(resp.text)
    except Exception as e:
        print(f"    Warning: could not fetch documents: {e}")
        return None


def view_url_for(documents):
    """The CSV's View URL for a record: its first document, or "" for none.

    `documents` is a list from get_documents. None is not a valid argument:
    "we could not find out" has no View URL to write, and the caller has to
    handle that rather than pass it through here.
    """
    if not documents:
        return ""
    return f"{BASE_URL}/Search/ViewDocument?D={documents[0]['token']}"


def get_view_url(detail_url):
    """The primary document's URL, keeping get_documents' three outcomes.

    Whether a record offers anything at all is a genuinely different question
    from what it offers, and the canary check only asks the first one.
    """
    documents = get_documents(detail_url)
    return None if documents is None else view_url_for(documents)


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
    """Fetch view URLs for a page of records in parallel, on search-free sessions.

    Kept alongside fetch_documents_parallel because the two answer different
    questions and the tests speak in this one's terms.
    """
    return list(_detail_pool.map(lambda r: get_view_url(r["detail_url"]), records))


def fetch_documents_parallel(records):
    """Every document for a page of records, in parallel on search-free sessions.

    One entry per record, each a list from get_documents (or None where the
    detail page could not be read).
    """
    return list(_detail_pool.map(lambda r: get_documents(r["detail_url"]), records))


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
            # The query string's own page parameter, wherever it sits in it. This
            # used to be int(href.split("page=")[-1]), which is right only while
            # page= is the last thing in the link: '?page=217&sort=BeginDate' made
            # the int() raise, was swallowed, and the whole pager read as absent --
            # one page, finished, silently. The state's links are bare today.
            try:
                page_num = int(parse_qs(urlsplit(link.get("href", "")).query).get("page", [""])[-1])
            except ValueError:
                continue    # not a page link, or not a number
            if last_page is None or page_num > last_page:
                last_page = page_num

    return records, last_page


def record_row(record, entity_name, status, view_url):
    """The CSV row for one record as the search grid listed it."""
    return [
        record["doc_number"],
        record["doc_type"],
        record["entity_code"],
        # The canonical dropdown name, not the site's rendering of it
        # (State grids shout it in caps: "DRY BEAN COMMISSION"). Two
        # things downstream match on this exact string: the resume
        # checkpoint, and build_site.py's ENTITIES guard, which
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
    ]


def scrape_entity(session, entity_name, entity_val, status, entity_type, doc_type, writer,
                  start_page=1, on_page=None, deadline=None,
                  record_filter=None, on_record_seen=None, on_documents=None):
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
    `on_documents`, when given, receives each page's documents.jsonl entries
    instead of their being appended -- --daily buffers them with the rows.
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
        documents_by_detail = dict(zip((r["detail_url"] for r in to_fetch),
                                       fetch_documents_parallel(to_fetch)))
        # The CSV keeps holding one URL per row -- its primary document -- while
        # the full list goes to documents.jsonl. Splitting them this way means
        # no CSV consumer has to change and the ~10% of records that publish
        # more than one document stop being invisible. See README.md.
        view_url_by_detail = {d: (None if docs is None else view_url_for(docs))
                              for d, docs in documents_by_detail.items()}
        document_entries = []

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
            writer.writerow(record_row(record, entity_name, status, view_url))
            document_entries.append(documents_entry(
                record["doc_number"], entity_name, record["detail_url"],
                documents_by_detail.get(record["detail_url"], [])))

        # Written only for rows that made it into the CSV, so the two files can
        # never disagree about which records were read. --daily holds both back
        # until it has judged the night (see run_daily), so it takes them instead.
        if on_documents is not None:
            on_documents(document_entries)
        else:
            append_documents(document_entries, os.path.join(ROOT, DOCUMENTS_JSONL))

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


def local_timestamp():
    """Now, as local time with an explicit UTC offset.

    The browser renders these in the reader's own timezone without guessing
    where the scrape ran. Shared with the daily diff report so an unfinished
    night's entries carry the same shape of stamp as a finished one's, without
    touching scrape_meta.json.
    """
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def set_aside_unreadable(path, error, keep_copy=True):
    """Say that a state file could not be read, and keep a copy for a person.

    The loaders that call this start fresh on an unreadable file rather than fail
    a scrape that has just succeeded -- the right call, but done silently it also
    drops whatever the file held: the other datasets' stamps (so the page's "Last
    updated" can fall back to the build date and hide a stale dataset), a diff
    report's unreviewed flips, or the full scrape's checkpoint (the only record of
    which CSV rows are legitimate). The warning is a GitHub annotation, so it lands
    on the run's summary page; the copy, named <file>.corrupt and overwritten by the
    next incident, keeps the evidence. --daily's own progress file is same-day
    resume state, cheap to rebuild, and takes the warning only.

    The copy is made first and only then claimed: if it cannot be made, the
    annotation says so rather than promising a file that is not there.
    """
    reason = " ".join(str(error).split())
    kept = ""
    if keep_copy:
        try:
            shutil.copyfile(path, path + ".corrupt")
            kept = f" A copy is kept as {path}.corrupt."
        except OSError as e:
            kept = f" A copy could not be kept ({' '.join(f'{type(e).__name__}: {e}'.split())})."
    print(f"::warning title=Unreadable state file::{path} could not be read ({reason}); "
          f"starting it fresh.{kept}")


def record_scrape_time(dataset):
    """Stamp this dataset's completion time, preserving stamps for other datasets."""
    stamps = {}
    if os.path.exists(SCRAPE_META):
        try:
            with open(SCRAPE_META, encoding="utf-8") as f:
                stamps = json.load(f)
            if not isinstance(stamps, dict):
                raise ValueError(f"expected an object, found {type(stamps).__name__}")
        except (OSError, ValueError) as e:
            set_aside_unreadable(SCRAPE_META, e)
            stamps = {}  # unreadable or corrupt: start fresh rather than fail the scrape

    stamps[dataset] = local_timestamp()

    # Beside and renamed over, like every other state file here, so a kill
    # mid-write cannot leave empty the one file "Last updated" is read from.
    tmp = SCRAPE_META + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stamps, f, indent=2, sort_keys=True)
    os.replace(tmp, SCRAPE_META)
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
    except (OSError, ValueError) as e:
        set_aside_unreadable(path, e)
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


# How far back from the end of a CSV repair_csv_tail looks for the last newline.
# A row is a few hundred bytes; the file is hundreds of megabytes.
CSV_TAIL_WINDOW = 1 << 20


# How much of a file is counted for quote characters at a time.
QUOTE_COUNT_CHUNK = 1 << 24


def odd_quotes_before(f, upto):
    """True if the first `upto` bytes of the open binary file hold an odd number of quote characters.

    csv.writer doubles every quote it escapes, so the rows of a whole file hold an even number:
    an odd one means the last field is still open.
    """
    f.seek(0)
    odd, left = False, upto
    while left > 0:
        chunk = f.read(min(QUOTE_COUNT_CHUNK, left))
        if not chunk:
            break
        left -= len(chunk)
        if chunk.count(b'"') % 2:
            odd = not odd
    return odd


def repair_csv_tail(csv_path):
    """Cut a half-written final row off the CSV, so the next append starts a new row.

    A job killed mid-write -- a runner lost, a SIGKILL -- leaves a last line with
    no newline. Both entry points append, so the next record would be glued onto
    that stump: one row of fifteen-odd fields that build_site.py cannot read. It
    exits on an unexpected view URL shape or raises on the Status for nearly every
    place the cut can fall, and the data cache and the hub-data release carry the
    row forward, so the nightly site build would fail every night until someone
    repaired the cached CSV by hand. The JSONL logs have always been repaired the
    same way on load; the CSV append path was not.

    Always cut, never keep: a stump can look like a whole row (cut just after the
    Detail URL it has eleven fields and an empty View URL), and an empty View URL
    says the record has no document -- a claim the state never made. A cut record
    is simply fetched again, because it is no longer known.

    Returns the number of bytes removed.

    Two cases it cannot repair stop the run (SystemExit) with the file untouched,
    instead of being guessed at. A megabyte with no newline in it is not a
    half-written row. And a file that, after the cut, would still end inside a
    quoted field -- an odd number of quote characters -- which is what a kill leaves
    in a field that contains a line break, and a search for the last newline cannot
    tell that line break from the end of a row: it would cut inside the field, or
    leave the open quote, and the next row appended would be swallowed by it. No
    field in the 744,100 rows of the local CSVs (a 17 Sep 2026 snapshot, scanned on
    14 Oct; the production copy in the Actions cache was not) holds a line break, so
    this has not been seen; if it is, it is loud here, not a garbled row in the build.
    """
    if not os.path.exists(csv_path):
        return 0
    with open(csv_path, "rb+") as f:
        size = f.seek(0, os.SEEK_END)
        if size == 0:
            return 0
        keep = size
        f.seek(size - 1)
        if f.read(1) != b"\n":
            window = min(size, CSV_TAIL_WINDOW)
            f.seek(size - window)
            newline = f.read(window).rfind(b"\n")
            if newline < 0 and window < size:
                sys.exit(f"ERROR: the last {window:,} bytes of {csv_path} hold no newline, which is "
                         "not a half-written row. Nothing was changed; the file needs a person to "
                         "look at it.")
            keep = size - window + newline + 1 if newline >= 0 else 0
        if odd_quotes_before(f, keep):
            sys.exit(f"ERROR: {csv_path} would still end inside a quoted field after repair (it has "
                     "an odd number of quote characters), so the next row appended would be "
                     "swallowed by it. A kill in a field with a line break in it leaves this, and "
                     "the repair cannot tell that line break from the end of a row. Nothing was "
                     "changed; the file needs a person to look at it.")
        if keep < size:
            f.truncate(keep)
    if keep < size:
        print(f"    Note: {csv_path} ends mid-row; cutting {size - keep:,} bytes of a partial row. "
              "That record is simply fetched again.")
    return size - keep


def csv_has_records(csv_path):
    """True if the CSV exists and holds at least one row below its header."""
    try:
        with open(csv_path, encoding="utf-8", errors="replace", newline="") as f:
            next(f, None)  # the header
            return any(line.strip() for line in f)
    except FileNotFoundError:
        return False


class UnaccountedRows(ValueError):
    """More rows belong to combos with no recorded position than a cut-off scrape can leave."""

    def __init__(self, count):
        super().__init__(f"{count:,} rows")
        self.count = count


# A scrape that is cut off leaves at most the page in flight unaccounted for -- 25 rows
# today. A thousand is ample slack for a larger page and still a sliver of a dataset. More
# than that means the progress file does not describe the CSV (unreadable, emptied, from
# another checkout), and dropping "unfinished" rows would delete the dataset.
MAX_UNACCOUNTED_ROWS = 1000


def drop_unfinished_rows(csv_path, keep, limit=None):
    """Strip rows belonging to combos we have no position for.

    A combo that is finished, or that has a recorded page position, has its
    rows accounted for and keeps them. Anything else was cut off before the
    first page was checkpointed, so its rows would be re-scraped and
    duplicated -- those are dropped.

    With `limit`, more than that many such rows raises UnaccountedRows and
    changes nothing. The file is rewritten beside itself and renamed over, so
    a kill mid-rewrite cannot leave a truncated CSV that every later check
    would accept.
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
    if limit is not None and dropped > limit:
        raise UnaccountedRows(dropped)

    if dropped:
        tmp = csv_path + ".tmp"
        try:
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(header)
                writer.writerows(kept)
            os.replace(tmp, csv_path)
        except BaseException:
            try:
                os.remove(tmp)      # a half-written copy is of no use to anyone
            except OSError:
                pass
            raise
    return dropped


# Fields worth watching for change, as (record key, CSV column, label).
# Deliberately not Status: --daily already derives Active -> Expired from
# presence, patches the CSV, and counts it in the diff report.
DOCUMENTS_JSONL = "data/documents.jsonl"


def document_key(detail_url):
    """A row's stable key, hashed from its full Detail URL.

    The full URL is the only thing that identifies a row, and every shorter key
    silently merges records. Measured over all 741,653 rows: 14,769 DN tokens
    map to more than one distinct detail URL, covering 29,597 rows, and
    (DN, document number) still collides on 14,957 pairs. record_changes() keys
    on the same URL for the same reason.

    Hashed rather than written out because at ~290 characters a row the keys
    alone would be 215 MB. Twelve bytes is far more than 741,605 rows need to
    stay collision-free, and the entry carries `doc` and `entity` besides, so
    the file is still greppable by the values a human would search for.
    """
    return hashlib.blake2b(detail_url.encode("utf-8"), digest_size=12).hexdigest()


def documents_entry(doc_number, entity_name, detail_url, documents, seen=None):
    """One documents.jsonl line. `documents` is get_documents' list, never None.

    Single-document rows -- ~90% of the corpus -- carry only the count, because
    their one document is already the CSV's View URL and repeating it here
    would double the file for nothing. Rows with more carry the whole list and
    their detail URL, which are the rows anyone actually opens this file to
    read.
    """
    entry = {
        "k": document_key(detail_url),
        "doc": doc_number,
        "entity": entity_name,
        "n": len(documents),
        "seen": seen or datetime.date.today().isoformat(),
    }
    if len(documents) > 1:
        entry["u"] = detail_url
        entry["documents"] = documents
    return entry


def append_documents(entries, out_path=DOCUMENTS_JSONL):
    """Append entries and get them onto the disk. Returns how many were written.

    fsync, not just flush, because this file is written by a run that is
    expected to be stopped -- a closed lid or a moved laptop should cost the
    rows in flight and nothing already reported as done.
    """
    if not entries:
        return 0
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return len(entries)


def load_documents(path=DOCUMENTS_JSONL):
    """Fold the log into {key: entry}, last entry winning.

    Fold, never count lines: this log is append-only and a re-check supersedes
    its earlier entry, so `wc -l` overstates by every row ever revisited. That
    exact mistake reported 23% complete as 40% once already.

    A truncated final line is expected rather than exceptional -- it is what a
    hard kill mid-write leaves behind -- so it is *truncated away*, not merely
    skipped. Skipping is not enough: the next run appends after the fragment,
    which turns it into a broken line in the middle of the file, and from then
    on every load would halt. Repairing debris once beats reading past it
    forever. A broken line anywhere earlier is left alone and does halt:
    something rewrote history there, and continuing would silently treat
    unchecked rows as done.
    """
    store = {}
    if not os.path.exists(path):
        return store

    with open(path, "rb") as f:
        raw = f.readlines()

    offset = 0
    for i, blob in enumerate(raw):
        start, offset = offset, offset + len(blob)
        line = blob.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if i == len(raw) - 1:
                print(f"    Note: {path} ends mid-write, which is what stopping "
                      f"the run abruptly leaves behind. Truncating {len(blob)} "
                      "bytes of a partial line; that row is simply checked again.")
                os.truncate(path, start)
                break
            raise SystemExit(
                f"{path} line {i + 1} is not valid JSON, and it is not the last "
                "line. Something rewrote this log rather than appending to it; "
                "refusing to run, because the rows after it cannot be trusted.")
        store[entry["k"]] = entry
    return store


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
    """Flip Status Active -> Expired for existing rows whose Detail URL is in flip_urls."""
    return patch_statuses(csv_path, flip_urls, set())[0]


def patch_statuses(csv_path, to_expired, to_active):
    """Flip Active -> Expired for rows whose Detail URL is in to_expired, and
    Expired -> Active for one row of each URL in to_active.

    `to_active` is a set of URLs, whose row only has its Status flipped, or a
    {url: row} dict, whose row is replaced by the one given -- --daily passes
    tonight's listing and View URL, so a record restored does not come back with
    the amount and end date it had when it left the list.

    Returns (rows flipped to Expired, {url: values of its row flipped back}),
    the values being the WATCHED_FIELDS columns as they stood before the patch,
    so the caller can record what moved while the record was off the list.

    One full rewrite, same shape as drop_unfinished_rows -- there is no index
    to seek by. Called once per dataset per --daily run, after every entity
    has been scraped, not once per entity.

    A URL in to_active is one with no Active row at all (see load_expired_only),
    so flipping one of its rows is what leaves it listed exactly once. The one
    flipped is its last: rows are only ever appended, so that is the row --daily
    most recently knew as Active, and an older Expired twin beside it -- the
    pairs the append-on-return behaviour left before Oct 2026 -- stays as it was.
    """
    if (not to_expired and not to_active) or not os.path.exists(csv_path):
        return 0, {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return 0, {}

    header, body = rows[0], rows[1:]
    patched = 0
    last_expired = {}
    for i, r in enumerate(body):
        if len(r) <= 9:
            continue
        if r[8] == "Active" and r[9] in to_expired:
            r[8] = "Expired"
            patched += 1
        elif r[8] == "Expired" and r[9] in to_active:
            last_expired[r[9]] = i

    replacements = to_active if isinstance(to_active, dict) else {}
    restored = {}
    for url, i in last_expired.items():
        restored[url] = tuple(body[i][column] for _key, column, _label in WATCHED_FIELDS)
        if replacements.get(url):
            body[i] = list(replacements[url])
        body[i][8] = "Active"

    if patched or restored:
        tmp = csv_path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(body)
        os.replace(tmp, csv_path)  # atomic, same reasoning as save_progress
    return patched, restored


def load_expired_only(csv_path):
    """document_key of every Detail URL the CSV holds as Expired and never as Active.

    These are the records an Active search should not list. --daily uses the set
    twice: a listed record in it is not fetched or appended (that append is what
    left a URL with both an Active and an Expired row), and how many of them are
    listed is what says whether tonight's Status filter held (see leak_suspected).

    Keys rather than URLs because nearly every row is Expired -- about 700,000
    URLs at ~290 characters each -- and a membership test is all this is for.
    """
    expired, active = set(), set()
    if not os.path.exists(csv_path):
        return expired
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for r in reader:
            if len(r) > 9:
                if r[8] == "Expired":
                    expired.add(document_key(r[9]))
                elif r[8] == "Active":
                    active.add(document_key(r[9]))
    return expired - active


DAILY_DIFF_REPORT = "data/daily_diff_report.json"

# The per-entity counts --daily reports, each summed into an entry's totals.
REPORT_COUNTS = ("previously_active", "still_active", "newly_active", "held_back",
                 "flipped_to_expired", "flipped_back")


def append_daily_diff_report(dataset, entity_report, stamp, complete=True):
    """Add one dataset-run's per-entity counts to the accumulating report.

    Consumed by scripts/check_daily_diff.py on the nightly publish leg, then
    cleared -- so in the ordinary case this file holds one evening's three
    entries. See that script for how the totals here get used as a guard rail.

    `complete` is False for a night some entity did not finish. Its entities'
    flips are already patched into the CSV, so they are reported anyway: from
    25 Sep 2026 one refused entity kept the state dataset unfinished for four
    nights, and the 300 flips patched on those nights reached no report and
    were never checked. A same-day re-run merges into that night's unfinished
    entry rather than adding another, and an entity already in it keeps its
    first counts -- those carry the real flips, and a re-scan after the patch
    would report none -- so no entity is ever counted twice in one night.
    """
    entries = []
    if os.path.exists(DAILY_DIFF_REPORT):
        try:
            with open(DAILY_DIFF_REPORT, encoding="utf-8") as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                raise ValueError(f"expected a list, found {type(entries).__name__}")
        except (OSError, ValueError) as e:
            set_aside_unreadable(DAILY_DIFF_REPORT, e)
            entries = []  # unreadable or corrupt: start the report fresh

    night = stamp[:10]  # local date, the same day load_daily_progress counts in
    entry = next((e for e in entries
                  if e.get("dataset") == dataset and not e.get("complete", True)
                  and str(e.get("timestamp", ""))[:10] == night), None)
    if entry is None:
        entry = {"dataset": dataset, "entities": {}}
        entries.append(entry)
    for name, counts in entity_report.items():
        entry["entities"].setdefault(name, counts)
    entry["timestamp"] = stamp
    entry["complete"] = complete
    # .get: an entry merged into tonight's may predate a count.
    entry["totals"] = {k: sum(e.get(k, 0) for e in entry["entities"].values()) for k in REPORT_COUNTS}

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
    except (OSError, ValueError) as e:
        set_aside_unreadable(path, e, keep_copy=False)
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


def flip_ledger_file(dataset):
    return f"data/{dataset}_daily_flips.json"


# A ledger entry older than this is dropped at load. A record --daily flipped that long ago
# and listed again is judged as a stray (see leak_suspected) instead of as a return it owes;
# at a handful per night that is under the leak line, and a stray that keeps being listed
# is reported (see LEAK_NIGHTS_LIMIT). Most flips are real endings that never come back, so
# without a limit the ledger only grew: about 80,000 entries a year (audit diff-03).
LEDGER_MAX_AGE_DAYS = 365

_DIGEST = re.compile(r"[0-9a-f]{24}")


def ledger_key(detail_url_or_key):
    """The ledger's key for a record: its document_key, like load_expired_only's set.

    The ledger held raw Detail URLs until Oct 2026, ~290 characters each. A key that is
    already a digest is returned as it is, so a ledger written before the change loads
    under the new keys instead of being lost -- losing it would make every record it owes
    a flip back look like a stray.
    """
    if _DIGEST.fullmatch(detail_url_or_key):
        return detail_url_or_key
    return document_key(detail_url_or_key)


def load_flip_ledger(path, expired_only=None, today=None):
    """{ledger_key: date flipped} for the records --daily itself flipped to Expired.

    Unlike the day's progress file this persists across nights: it is how a record listed
    again is told apart from one the Status filter let through (see leak_suspected).
    Losing it costs no rows, but it is not free: every record it owed a flip back then
    counts as a stray, and enough strays at one entity hold back its flip-backs as a leak
    -- night after night, since they do not go away. That is what LEAK_NIGHTS_LIMIT turns
    into a red run. An unreadable ledger is kept aside and started fresh.

    Pruned as it loads, when `expired_only` (load_expired_only, read at the start of the
    same run) is given: an entry whose record the CSV no longer holds as Expired-only is
    owed nothing -- it was flipped back, re-read Active by a full scrape, or is what a kill
    between the ledger write and the CSV patch left -- and an entry older than
    LEDGER_MAX_AGE_DAYS is dropped. Never prune after tonight's flips are added: their
    records were Active when expired_only was read.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError(f"expected an object, found {type(raw).__name__}")
    except (OSError, ValueError) as e:
        set_aside_unreadable(path, e)
        return {}
    ledger = {ledger_key(k): v for k, v in raw.items()}
    if expired_only is None:
        return ledger
    today = today or datetime.date.today()
    oldest = (today - datetime.timedelta(days=LEDGER_MAX_AGE_DAYS)).isoformat()
    # An unreadable date is kept: the age limit is housekeeping, and dropping an entry
    # costs a stray, so doubt keeps it.
    return {k: v for k, v in ledger.items()
            if k in expired_only and not (isinstance(v, str) and len(v) == 10 and v < oldest)}


def save_flip_ledger(path, ledger):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=0, sort_keys=True)
    os.replace(tmp, path)


# How many active records an entity needs before "all of them vanished" is
# refused rather than believed. Without a floor, one record was enough: the
# Coordinating Commission for Postsecondary Education had a single active
# record, the state stopped listing it on 25 Sep 2026, and the refusal turned
# that one ending into a night that never finished -- four nights running.
# The scrape time and the diff report were written only on a finished night,
# so the state dataset's "last updated" froze at 24 Sep and 300 flips patched
# on those nights were never checked. Five matches check_daily_diff.py's
# MIN_BASELINE, below which it too calls a percentage noise, and it is what
# lets a small body genuinely ending its four contracts flip.
#
# The price: an outage that answers empty for every entity now flips each
# entity with one to four active records to Expired -- 17 state entities, 29
# records, on the 28 Sep 2026 release. Until Oct 2026 nothing put those rows
# back: a record listed again was unknown and appended as a new Active row
# beside its stale Expired one. Now it is flipped back (see leak_suspected),
# and dataset_breaker below keeps such a night from flipping them at all.
ALL_GONE_FLOOR = 5


def refuse_flips(known, seen):
    """Why tonight's disappearances for one entity cannot be trusted, or None.

    An entity that returns nothing is the state failing, not 20,000 contracts
    ending overnight. On 17-18 Aug 2026 its search answered "No results found"
    for every entity for two days; scrape_entity treats an empty first page as
    a clean finish, so `seen` came back empty, `known - seen` was everything,
    and a single run marked all 44,063 active records in the database Expired.
    Nothing failed, and nothing could undo it: --daily then only ever flipped
    Active -> Expired.

    So a flip has to be evidence of an ending, not of an absence. Refuse the
    two shapes that cannot be real -- five or more active records all gone,
    or more than half of 20+ gone at once -- and re-check next run.
    """
    if len(known) >= ALL_GONE_FLOOR and not seen:
        return (f"saw 0 records but {len(known):,} were active -- refusing to expire them. "
                "The state is answering empty, not reporting an ending. Will re-check next run.")
    vanished = known - seen
    share = len(vanished) / len(known) if known else 0
    if len(known) >= 20 and share > 0.5:
        return (f"{len(vanished):,} of {len(known):,} active records ({share:.0%}) vanished "
                "at once -- refusing to expire them. Will re-check next run.")
    return None


# The same question asked of the whole dataset. refuse_flips judges one entity at a time, and
# an entity with fewer than ALL_GONE_FLOOR active records is allowed to lose all of them --
# a small body's last contract ending is real. So on a night the state answers empty for
# everything, the large entities are refused and the small ones (17 of them, 29 records, in
# the 28 Sep 2026 release) flip to Expired with nothing to say the night was bad, and stay so
# until the state lists them again. More than half of a
# dataset's active records vanishing in one night is not contracts ending: in the local 17 Sep
# 2026 snapshot, apart from the open-ended 2099 placeholder date, no end date is shared by more
# than 3% of a dataset's active records, and under 0.1% are still Active past their end date.
# So past that point nothing is flipped for any entity. That also holds back the others'
# endings when one entity holding over half of a dataset is refused on its own (the University
# of Nebraska Lincoln holds 52% of the contracts and 61% of the purchase orders); the night is
# unfinished anyway, and those flips come the next night, once the state answers.
DATASET_BREAKER_FLOOR = 50      # known-active records in the entities finished tonight, below which a share means little
DATASET_BREAKER_SHARE = 0.5


def dataset_breaker(known, vanished):
    """Why tonight's disappearances, across every entity read, cannot be trusted, or None."""
    if known >= DATASET_BREAKER_FLOOR and vanished > known * DATASET_BREAKER_SHARE:
        return (f"{vanished:,} of {known:,} active records ({vanished / known:.0%}) vanished across "
                "the dataset in one night -- the state is failing, not reporting endings. Flipping "
                "nothing tonight, for any entity, small ones included. Will re-check next run.")
    return None


# A record --daily flipped to Expired is flipped back when the Active search lists it again.
# Until Oct 2026 nothing did that: the record was unknown, so it was fetched and appended as a
# new Active row beside its stale Expired one, and the CSV listed it twice. Flipping back trusts
# the same search whose silence made the flip, so it has to know when that search is wrong the
# other way: when its Status filter lets Expired records through. Such a "leak" lists records
# the CSV holds as Expired that --daily never flipped -- the full scrape read them from the
# Expired search -- and those far outnumber the Active ones (about 700,000 of 741,653 rows). So
# the leak is judged on those strays alone, never on how many of --daily's own flips came back:
# after an outage spread over two nights all of those come back at once, and refusing that
# would turn the outage into a freeze. A few strays are a contract genuinely re-opened and are
# flipped back too; past the line nothing is flipped back, for the entity or, judged across
# every entity read tonight, for the whole dataset. A leaky night is still finished and stamped:
# what it lists adds to `seen`, so its endings are as sound as any night's, and holding it open
# would freeze the dataset for as long as the state's filter stays broken. It warns instead,
# until the leak has held for LEAK_NIGHTS_LIMIT nights (below).
LEAK_FLOOR = 5          # strays below this are never a leak, whatever the share
LEAK_SHARE = 0.05       # ...and at or above it, a leak once they are more than this share of what was listed


def leak_suspected(strays, listed):
    """Why tonight's Active search cannot be trusted to flip anything back, or None.

    `strays`: listed records the CSV holds only as Expired, which --daily never flipped.
    `listed`: every record the search listed.
    """
    if strays >= LEAK_FLOOR and strays > listed * LEAK_SHARE:
        return (f"the Active search listed {strays:,} record(s) the state had already filed as "
                f"Expired, of {listed:,} listed -- its Status filter is letting Expired records "
                "through. Flipping nothing back, and holding back the records the CSV has never seen "
                "rather than appending them as Active. Will re-check next run.")
    return None


# Strays that never go away freeze flip-backs for good. They are not always a broken filter:
# a lost or pruned ledger, or a record --daily flipped before the ledger existed (Oct 2026),
# makes a record it owes look like a stray, and it is listed again every night. Before this
# the leak warning simply repeated, nightly and green, while the entity's flip-backs never
# happened (audit diff-01). Persistence cannot say which it is -- a filter that stays broken
# lists the same strays every night too -- so it is not taken as proof either way: after
# this many leak nights in a row for one entity, or for the dataset, the dataset is left
# unstamped, which turns the run red (check_scrape_stamps.py), and a person decides. If
# the strays are genuine, `--daily --entity X --accept-strays` restores them.
LEAK_NIGHTS_LIMIT = 3
LEAK_SAMPLE = 5             # stray document numbers kept per entity, for a person to look up
DATASET_STREAK = "(across the dataset)"


def leak_streak_file(dataset):
    return f"data/{dataset}_daily_leaks.json"


def load_leak_streaks(path):
    """{entity name or DATASET_STREAK: {"nights": n, "last": date, "strays": [doc, ...]}}."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError(f"expected an object, found {type(raw).__name__}")
    except (OSError, ValueError) as e:
        set_aside_unreadable(path, e)
        return {}
    return raw


def save_leak_streaks(path, streaks):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(streaks, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def note_leak_night(streaks, name, leaky, today, strays=(), force_reset=False):
    """Count one judged night into `name`'s streak: a leak adds a night, a clean night ends it.

    A second judgement the same day changes nothing, so a same-day re-run neither adds a
    night nor wipes one out. Only judged nights reach here -- a refused entity, an
    unfinished one and a breaker night say nothing about the filter and leave it alone.
    """
    entry = streaks.get(name)
    if force_reset or not leaky:
        if entry is not None and (force_reset or entry.get("last") != today):
            del streaks[name]
        return
    if entry is not None and entry.get("last") == today:
        return
    streaks[name] = {"nights": (entry or {}).get("nights", 0) + 1, "last": today,
                     "strays": list(strays)[:LEAK_SAMPLE]}


def persistent_leaks(streaks, dataset_entities):
    """The streaks at or past LEAK_NIGHTS_LIMIT, among this dataset's entities and its own."""
    return {name: e for name, e in streaks.items()
            if (name in dataset_entities or name == DATASET_STREAK)
            and isinstance(e, dict) and e.get("nights", 0) >= LEAK_NIGHTS_LIMIT}


def run_daily(args):
    """Scrape only Status=Active for one dataset and diff against what the
    existing CSV already knows is Active -- see README.md, "Daily updates"
    for why this is safe (build_site.py's exact-fingerprint dedup) and what
    it deliberately does not catch (amendments to an already-Active record).
    """
    entity_type, doc_type, output_csv = DATASETS[args.dataset]
    is_state = args.dataset == "state"
    entities = STATE_ENTITIES if is_state else HIGHER_ED_ENTITIES
    # "Complete" means every entity of the dataset was read today, whatever subset this run
    # is given: `--daily --entity X` finishing X used to stamp the whole dataset fresh.
    dataset_entities = entities

    if args.entity:
        unknown = [e for e in args.entity if e not in entities]
        if unknown:
            known = "scripts/state_entities.json" if is_state else "HIGHER_ED_ENTITIES in scripts/scrape.py"
            print(f"unknown entity name(s) for dataset {args.dataset!r}: {unknown}. See {known}.", file=sys.stderr)
            sys.exit(2)
        entities = {name: entities[name] for name in args.entity}

    os.makedirs("data", exist_ok=True)

    # Before the baseline is read and before anything is appended, so a record
    # whose row was cut short counts as unknown tonight and is fetched again.
    repair_csv_tail(output_csv)

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
    accept_strays = bool(getattr(args, "accept_strays", False))
    if accept_strays:
        # A deliberate re-read of entities tonight's run may already have finished.
        done_today.difference_update(entities)
    streaks_path = leak_streak_file(args.dataset)
    streaks = load_leak_streaks(streaks_path)
    ledger_path = flip_ledger_file(args.dataset)
    expired_only = load_expired_only(output_csv)
    ledger = load_flip_ledger(ledger_path, expired_only)

    write_header = not os.path.exists(output_csv) or os.path.getsize(output_csv) == 0
    if write_header:
        with open(output_csv, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)
    start = time.time()
    deadline = start + args.hours * 3600 if args.hours else None

    flips = {}    # entity_name -> set(detail_url) flipped to Expired
    backs = {}    # entity_name -> {detail_url: record} flipped back to Active
    pending = {}  # entity_name -> (rows, documents entries, new-record count, leaky), held until the night is judged
    report = {}   # entity_name -> counts dict
    samples = {}  # entity_name -> sorted document numbers of tonight's strays
    amended = 0   # watched fields seen to have moved, written to changes.jsonl
    scanned_known = scanned_vanished = 0   # every entity read tonight, refused ones too
    listed_total = strays_total = 0        # every entity finished and not refused tonight

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
        returned = {}   # detail_url -> record, for listed records the CSV holds only as Expired
        rows = HeldRows()
        documents = []

        def note_change(record):
            nonlocal amended
            amended += record_changes(record, baseline)

        def note_seen(record, _seen=seen, _returned=returned):
            _seen.add(record["detail_url"])
            if document_key(record["detail_url"]) in expired_only:
                _returned[record["detail_url"]] = record
            else:
                note_change(record)

        count, finished = scrape_entity(
            session, entity_name, entity_val, "Active", entity_type, doc_type, rows,
            deadline=deadline,
            # Neither a known Active record nor one the CSV holds as Expired is
            # fetched or appended: the first is unchanged, the second is flipped
            # back below -- fetched then, once the night is judged -- or, on a
            # leaky night, left alone. Either way its URL keeps exactly one row.
            record_filter=lambda r, _k=known: (r["detail_url"] not in _k
                                               and document_key(r["detail_url"]) not in expired_only),
            on_record_seen=note_seen,
            on_documents=documents.extend,
        )

        if not finished:
            # Deadline or a recovery failure -- don't mark done, don't compute
            # flips off an incomplete "seen" set. It retries from page 1 next
            # run (today's retry, or tomorrow's fresh run). Its new rows go too:
            # half a listing cannot be judged for a leak, and the re-scan finds them again.
            print(f"  {entity_name}: not finished this run, will re-scan from the top later."
                  + (f" Its {len(rows):,} new record(s) are not written yet." if rows else ""))
            continue

        scanned_known += len(known)
        scanned_vanished += len(known - seen)

        stray_docs = sorted(r["doc_number"] for u, r in returned.items() if ledger_key(u) not in ledger)
        strays = len(stray_docs)
        appended = seen - known - set(returned)
        leak = leak_suspected(strays, len(seen))
        if leak and accept_strays:
            print(f"  {entity_name}: --accept-strays: taking {strays:,} stray(s) as genuinely "
                  f"re-opened and flipping them back: {', '.join(stray_docs[:LEAK_SAMPLE])}"
                  f"{'...' if strays > LEAK_SAMPLE else ''}")
            leak = None
            strays = 0
        samples[entity_name] = stray_docs
        # Its new rows wait for both leak verdicts, this entity's and the dataset's: a leaking
        # filter lists Expired records the CSV has never seen as readily as ones it has, and
        # appending those would publish them as Active (audit sc-04).
        pending[entity_name] = (rows, documents, len(appended), bool(leak))

        # A refused entity is re-scanned next run, and nothing of it is
        # patched or reported tonight.
        refusal = refuse_flips(known, seen)
        if refusal:
            print(f"  {entity_name}: {refusal}")
            continue

        listed_total += len(seen)
        strays_total += strays
        if leak:
            print(f"::warning title=Daily scrape: the Active search is listing Expired records::"
                  f"{args.dataset}: {entity_name}: {leak}")
            returned = {}

        vanished = known - seen
        report[entity_name] = {
            "previously_active": len(known),
            "still_active": len(seen & known),
            "newly_active": len(appended),
            "held_back": 0,
            "flipped_to_expired": len(vanished),
            "flipped_back": len(returned),
        }
        flips[entity_name] = vanished
        backs[entity_name] = returned

        done_today.add(entity_name)
        save_daily_progress(progress_path, done_today)

    if amended:
        print(f"Recorded {amended:,} field change(s) to {CHANGES_JSONL}.")

    distrust = dataset_breaker(scanned_known, scanned_vanished)
    if distrust:
        print(f"::error title=Daily scrape: the state looks like it is answering empty::"
              f"{args.dataset}: {distrust}")
        # Every entity whose flips and report entry are dropped is not done: left in done_today
        # it would be skipped by a same-day re-run, which could then finish the night and stamp
        # it complete over flips that were never made.
        done_today.difference_update(flips)
        save_daily_progress(progress_path, done_today)
        flips.clear()
        backs.clear()
        report.clear()

    # The same leak test across the dataset, for a filter leaking a few records into each of
    # many entities. The entities stay finished, as a leaky entity does; only their flip-backs
    # and new rows go.
    dataset_leak = leak_suspected(strays_total, listed_total)
    if dataset_leak and (any(backs.values()) or any(p[2] for p in pending.values())):
        print(f"::warning title=Daily scrape: the Active search is listing Expired records::"
              f"{args.dataset}: across the dataset, {dataset_leak}")
        for name in backs:
            report[name]["flipped_back"] = 0
        backs.clear()

    write_new_rows(output_csv, pending, report, held=lambda name: pending[name][3] or bool(dataset_leak))

    today = datetime.date.today().isoformat()
    for name in report:                     # judged tonight: finished, not refused, no breaker
        note_leak_night(streaks, name, pending[name][3], today, samples.get(name, ()),
                        force_reset=accept_strays)
    if report:
        note_leak_night(streaks, DATASET_STREAK, bool(dataset_leak), today,
                        sorted(d for n in report for d in samples.get(n, ()))[:LEAK_SAMPLE],
                        force_reset=accept_strays)
    save_leak_streaks(streaks_path, streaks)
    persistent = persistent_leaks(streaks, dataset_entities)
    for name, e in sorted(persistent.items()):
        print(f"::error title=Daily scrape: flip-backs held back {e['nights']} nights running::"
              f"{args.dataset}: {name}: the Active search has listed records the CSV holds as "
              f"Expired on {e['nights']} judged nights in a row (last {e['last']}), so none of its "
              "flip-backs or new records have been applied. Either the state's Status filter is "
              "still leaking, or these are records the flip ledger no longer names -- look up "
              f"some of them: {', '.join(e.get('strays', [])) or 'none recorded'}. If they are "
              f"genuinely Active again: scrape.py {args.dataset} --daily --entity "
              f"\"<entity>\" --accept-strays. The dataset stays unstamped until then.")

    all_flip_urls = {u for s in flips.values() for u in s}
    all_backs = {u: (name, r) for name, b in backs.items() for u, r in b.items()}
    if all_flip_urls or all_backs:
        # A returning record is restored as tonight's listing shows it, with a fresh detail
        # fetch, the same as a new record: until Oct 2026 only its Status moved, so it came
        # back with the amount, end date and View URL it had when it left (audit diff-02).
        fresh, fresh_documents = fresh_rows(all_backs)
        # Into the ledger before the CSV, out of it after: a kill in between leaves a ledger
        # entry with no Expired row, which is pruned at the next load, rather than an Expired
        # row the ledger forgot, which would count against the next night as a stray.
        if all_flip_urls:
            ledger.update((ledger_key(u), datetime.date.today().isoformat()) for u in all_flip_urls)
            save_flip_ledger(ledger_path, ledger)
        patched, restored = patch_statuses(output_csv, all_flip_urls, fresh)
        if all_flip_urls:
            print(f"Patched {patched:,} row(s) Active -> Expired.")
        if all_backs:
            print(f"Patched {len(restored):,} row(s) Expired -> Active: listed again tonight, "
                  "with tonight's values and documents.")
            for name, b in backs.items():
                report[name]["flipped_back"] = sum(1 for u in b if u in restored)
            for url in restored:
                ledger.pop(ledger_key(url), None)
            save_flip_ledger(ledger_path, ledger)
            append_documents([e for u, e in fresh_documents.items() if u in restored],
                             os.path.join(ROOT, DOCUMENTS_JSONL))
            moved = sum(record_changes(all_backs[u][1], restored) for u in restored)
            if moved:
                print(f"Recorded {moved:,} field change(s) to {CHANGES_JSONL} on records listed again.")

    if all(e in done_today for e in dataset_entities) and persistent:
        # Every entity was read, but the run has to go red: check_scrape_stamps.py is the only
        # step in daily.yml that can fail the run without blocking the deploy after it.
        append_daily_diff_report(args.dataset, report, local_timestamp(), complete=False)
        print(f"\nDaily scrape read every entity of {args.dataset}, but its scrape time is not "
              f"recorded: {len(persistent)} leak streak(s) reached {LEAK_NIGHTS_LIMIT} nights "
              "(see the ::error lines above).")
    elif all(e in done_today for e in dataset_entities):
        stamp = record_scrape_time(args.dataset)
        append_daily_diff_report(args.dataset, report, stamp)
        if os.path.exists(progress_path):
            os.remove(progress_path)  # today is done; tomorrow starts fresh anyway, but tidy
        print(f"\nDaily scrape complete for {args.dataset}. Scrape time recorded: {stamp}")
    else:
        # The flips above are in the CSV whether or not the night finishes, so
        # the guard rail has to see them tonight -- but the scrape time stays
        # unrecorded, because "last updated" must never claim an entity that
        # was not read.
        if report:
            append_daily_diff_report(args.dataset, report, local_timestamp(), complete=False)
        remaining = sorted(set(dataset_entities) - done_today)
        print(f"\nDaily scrape incomplete -- {len(remaining)} entit(y/ies) not finished today: "
              f"{remaining[:5]}{'...' if len(remaining) > 5 else ''}. The {len(report)} "
              "finished this run are patched and in the diff report; the scrape time is "
              "recorded only once every entity of the dataset finishes. "
              + ("This run was limited by --entity, so the rest of the dataset was not read; "
                 "a run without --entity finishes it." if args.entity else "Re-run to finish."))


class HeldRows(list):
    """A csv.writer stand-in that keeps an entity's new rows until the night is judged."""

    def writerow(self, row):
        self.append(row)


def write_new_rows(csv_path, pending, report, held):
    """Append the new rows of every entity whose listing passed the leak checks.

    `pending` is run_daily's {entity: (rows, documents entries, new records, leaky)} and
    `held(entity)` says whether that entity's rows are held back tonight. A held entity's
    new records are reported as held_back rather than newly_active; they are not written,
    so they are unknown to the CSV tomorrow and fetched and judged again then.
    """
    rows, documents, held_back = [], [], 0
    for name, (entity_rows, entity_documents, new, _leaky) in pending.items():
        if held(name):
            held_back += new
            if name in report:
                report[name]["newly_active"] = 0
                report[name]["held_back"] = new
        else:
            rows.extend(entity_rows)
            documents.extend(entity_documents)
    if held_back:
        print(f"Held back {held_back:,} record(s) the CSV has never seen, listed by an Active search "
              "that is letting Expired records through: not appended tonight, judged again next run.")
    if rows:
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
            f.flush()
            os.fsync(f.fileno())
    # After the rows, as scrape_entity does: a documents entry never names a row that is not there.
    append_documents(documents, os.path.join(ROOT, DOCUMENTS_JSONL))
    return len(rows), held_back


def fresh_rows(backs):
    """Tonight's CSV row and documents entry for each record to flip back.

    `backs` is {detail_url: (entity_name, record as listed)}. Returns ({url: row}, {url:
    documents entry}); a record whose detail page could not be read is left out of both,
    so it stays Expired and owed tonight and is retried next run, the same as a new record
    whose detail page fails is not written.
    """
    if not backs:
        return {}, {}
    items = list(backs.items())
    rows, entries, deferred = {}, {}, 0
    for (url, (name, record)), docs in zip(items, fetch_documents_parallel([r for _u, (_n, r) in items])):
        if docs is None:
            deferred += 1
            continue
        rows[url] = record_row(record, name, "Active", view_url_for(docs))
        entries[url] = documents_entry(record["doc_number"], name, url, docs)
    if deferred:
        print(f"  {deferred:,} record(s) listed again could not have their detail pages read: "
              "left Expired tonight, retried next run.")
    return rows, entries


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
    # Line-buffered even when stdout is a pipe, as it is in Actions: block-buffered, a night's
    # progress lines arrived in the log minutes late and out of order with what stderr said,
    # and a run killed at its time limit lost the tail that said where it had got to.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)
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
    parser.add_argument("--accept-strays", action="store_true",
                        help="With --daily --entity: take the records the Active search lists that "
                             "the CSV holds as Expired as genuinely re-opened, flip them back, and "
                             "end those entities' leak streaks. For use by hand, after looking.")
    args = parser.parse_args()

    if args.accept_strays and not (args.daily and args.entity):
        parser.error("--accept-strays needs --daily and at least one --entity: it overrides the "
                     "leak check, so it is given only for entities a person has looked at.")
    if args.daily:
        if args.fresh or args.status:
            parser.error("--daily is incompatible with --fresh/--status (it always reads and "
                          "patches the existing CSV in place; it never starts fresh).")
        run_daily(args)
        return

    entity_type, doc_type, output_csv = DATASETS[args.dataset]
    is_state = args.dataset == "state"

    entities = STATE_ENTITIES if is_state else HIGHER_ED_ENTITIES
    dataset_entities = entities   # "complete" means all of these, whatever subset this run is given

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

    # Repair a torn tail before judging what the CSV holds: a header and one killed row
    # would otherwise count as a record, and be cut to nothing a moment later.
    repair_csv_tail(output_csv)

    # A progress file that vouches for finished work the CSV does not hold means
    # data/ was not restored: the progress files are tracked in git and the CSVs
    # are not, so this is exactly what a fresh clone looks like. Carrying on would
    # scrape only what the file says is left, write it under a fresh header, and
    # stamp the dataset complete -- an empty one, as of now. (If the finished
    # combos genuinely had no records, --fresh is the way past it.)
    if (done or partial) and not csv_has_records(output_csv):
        print(f"ERROR: {progress_path} records {len(done) + len(partial):,} combo(s) as finished "
              f"or under way, but {output_csv} "
              f"{'does not exist' if not os.path.exists(output_csv) else 'has no records'}. "
              "That is what a fresh clone looks like (the progress files are tracked in git, "
              "the CSVs are not). Nothing was scraped. Restore data/ first (README: \"Getting "
              "the data back\"), or pass --fresh to collect everything again (20+ hours) -- "
              "which is also the way past it if those combos really had no records.",
              file=sys.stderr)
        sys.exit(1)

    print(f"{len(todo):,} (entity, status) combos remaining of {len(all_combos):,}")

    try:
        dropped = drop_unfinished_rows(output_csv, done | set(partial), limit=MAX_UNACCOUNTED_ROWS)
    except UnaccountedRows as e:
        print(f"ERROR: {output_csv} holds {e.count:,} rows for combos that {progress_path} has no "
              "record of. A scrape that was cut off leaves at most the page in flight (25 rows), "
              "so this is not a cut-off: the progress file is probably unreadable, empty or from "
              "another checkout, and dropping these rows would delete the dataset. Nothing was "
              "changed. Restore the checkpoint (git checkout it; a copy of an unreadable one is "
              "kept beside it as .corrupt), or pass --fresh to discard the CSV and start over.",
              file=sys.stderr)
        sys.exit(1)
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

    # "Complete" is a fact about the whole dataset, not about the entities this run was
    # asked for, and the stamp says when it was last pulled: --entity X finishing X is not
    # the dataset finishing, and a run that found every combo already done pulled nothing.
    # Either used to record the time as now, so a debugging run or a re-run on a restored
    # dataset made stale data read as freshly scraped -- the inverse of the 24 Sep 2026
    # incident, where fresh data read as stale.
    remaining_here = len([c for c in all_combos if (c[0], c[2]) not in done])
    remaining = len([c for c in combos(dataset_entities) if (c[0], c[2]) not in done])
    if remaining_here:
        print(f"\nStopped with {remaining_here:,} combos remaining. Re-run to resume.")
    elif remaining:
        print(f"\nThis run's combos are done, but {remaining:,} of the whole dataset's are still "
              "to do, so its scrape time is left as it was.")
    elif todo:
        stamp = record_scrape_time(args.dataset)
        print(f"\nAll combos complete. Scrape time recorded: {stamp}")
    else:
        print("\nNothing to do: every combo was already complete, so the scrape time is left as it was.")

    print(f"Records this run: {grand_total:,}")
    print(f"Saved to: {output_csv}")


if __name__ == "__main__":
    main()
