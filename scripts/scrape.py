import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import argparse
import datetime
import json
import threading
import time
import csv
import os

BASE_URL = "https://statecontracts.nebraska.gov"
SEARCH_URL = f"{BASE_URL}/Search"
RESULTS_URL = f"{BASE_URL}/Search/SearchResults"

ENTITIES = {
    "University of Nebraska Lincoln": "University of Nebraska Lincoln-051-002",
    "University of Nebraska Central Administration": "University of Nebraska Central Administration-051-009",
}

STATUSES = ["Active", "Expired"]

# Which Higher Education document type to scrape, and where it lands.
DOC_TYPES = {
    "purchase-order": ("Purchase Order", "data/nu_purchase_orders.csv"),
    "contract": ("Contract", "data/nu_contracts.csv"),
}

# Completion times, one per document type, so the published page can say how
# fresh its data is. Each doc type is scraped by a separate run, so this file
# accumulates a stamp per type rather than being overwritten wholesale.
SCRAPE_META = "data/scrape_meta.json"

# Set to True to append to an existing CSV instead of overwriting
APPEND_MODE = False

# Skip specific (entity_name, status) combos — useful when resuming
SKIP_COMBOS = set()
# e.g. SKIP_COMBOS = {("University of Nebraska Lincoln", "Active")}

# Number of parallel workers for detail page fetches
MAX_WORKERS = 15

# Politeness delays, in seconds
DETAIL_DELAY = 0.15
PAGE_DELAY = 1.0


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
    """Fetch the detail page and return the View URL, or empty string if unavailable."""
    if not detail_url:
        return ""
    try:
        time.sleep(DETAIL_DELAY)
        resp = detail_session().get(detail_url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        view_link = soup.find("a", href=lambda h: h and "ViewDocument" in h)
        return BASE_URL + view_link["href"] if view_link else ""
    except Exception as e:
        print(f"    Warning: could not fetch view URL: {e}")
        return ""


_detail_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)


def fetch_view_urls_parallel(records):
    """Fetch view URLs for a page of records in parallel, on search-free sessions."""
    return list(_detail_pool.map(lambda r: get_view_url(r["detail_url"]), records))


def parse_results_page(soup):
    """Parse a results page. Returns (records, last_page_number)."""
    records = []
    table = soup.find("table", id="entitygrid")
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


def scrape_entity(session, entity_name, entity_val, status, doc_type, writer):
    print(f"\nScraping: {entity_name} | {status} | {doc_type}")

    token = get_token(session)
    data = {
        "__RequestVerificationToken": token,
        "TempEntity": entity_val,
        "Status": status,
        "Type": "Higher Education",
        "Entity": entity_val,
        "DocType": doc_type,
        "Vendor": "",
        "Amount": "0",
    }

    time.sleep(PAGE_DELAY)
    resp = session.post(RESULTS_URL, data=data, timeout=60)
    resp.raise_for_status()

    page = 1
    total = 0

    while True:
        soup = BeautifulSoup(resp.text, "html.parser")

        if "No results found" in resp.text:
            print("  No results found.")
            break

        records, last_page = parse_results_page(soup)

        if not records:
            print(f"  Page {page}: no rows found, stopping.")
            break

        # Fetch all view URLs for this page in parallel
        view_urls = fetch_view_urls_parallel(records)

        for record, view_url in zip(records, view_urls):
            writer.writerow([
                record["doc_number"],
                record["doc_type"],
                record["entity_code"],
                record["entity_name"],
                record["vendor"],
                record["amount"],
                record["begin_date"],
                record["end_date"],
                status,
                record["detail_url"],
                view_url,
            ])

        total += len(records)
        print(f"  Page {page}: {len(records)} records (running total: {total})")

        if last_page is None or page >= last_page:
            break

        page += 1
        time.sleep(PAGE_DELAY)
        resp = session.get(f"{RESULTS_URL}?page={page}", timeout=60)
        resp.raise_for_status()

    return total


def record_scrape_time(doc_type):
    """Stamp this document type's completion time, preserving stamps for other types."""
    stamps = {}
    if os.path.exists(SCRAPE_META):
        try:
            with open(SCRAPE_META, encoding="utf-8") as f:
                stamps = json.load(f)
        except (OSError, ValueError):
            stamps = {}  # unreadable or corrupt: start fresh rather than fail the scrape

    # Local time with an explicit UTC offset, so the browser can render it in
    # the reader's own timezone without guessing where the scrape ran.
    stamps[doc_type] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    with open(SCRAPE_META, "w", encoding="utf-8") as f:
        json.dump(stamps, f, indent=2, sort_keys=True)
    return stamps[doc_type]


def main():
    parser = argparse.ArgumentParser(description="Scrape NU documents from the Nebraska State Contracts Database.")
    parser.add_argument("doc_type", nargs="?", default="purchase-order", choices=sorted(DOC_TYPES),
                        help="Which document type to scrape (default: purchase-order)")
    args = parser.parse_args()
    doc_type, output_csv = DOC_TYPES[args.doc_type]

    os.makedirs("data", exist_ok=True)

    file_mode = "a" if APPEND_MODE else "w"
    write_header = not APPEND_MODE or not os.path.exists(output_csv)

    with open(output_csv, file_mode, newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "Document Number", "Document Type", "Entity Code", "Entity Name",
                "Vendor", "Amount", "Begin Date", "End Date", "Status", "Detail URL", "View URL"
            ])

        session = build_session()

        grand_total = 0
        for entity_name, entity_val in ENTITIES.items():
            for status in STATUSES:
                if (entity_name, status) in SKIP_COMBOS:
                    print(f"\nSkipping: {entity_name} | {status}")
                    continue
                count = scrape_entity(session, entity_name, entity_val, status, doc_type, writer)
                grand_total += count
                f.flush()

    stamp = record_scrape_time(doc_type)

    print(f"\nDone. Total records: {grand_total}")
    print(f"Saved to: {output_csv}")
    print(f"Scrape time recorded: {stamp}")


if __name__ == "__main__":
    main()
