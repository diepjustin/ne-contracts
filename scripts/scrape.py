import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import argparse
import datetime
import json
import statistics
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
    """Fetch the detail page and return the View URL, or empty string if unavailable."""
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
        return ""


def drain_latency():
    """Median detail-fetch seconds since the last call, or None if nothing was fetched."""
    with _latency_lock:
        if not _latencies:
            return None
        median = statistics.median(_latencies)
        _latencies.clear()
        return median


_detail_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)


def fetch_view_urls_parallel(records):
    """Fetch view URLs for a page of records in parallel, on search-free sessions."""
    return list(_detail_pool.map(lambda r: get_view_url(r["detail_url"]), records))


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


def scrape_entity(session, entity_name, entity_val, status, entity_type, doc_type, writer):
    label = f"{entity_name} | {status}" + (f" | {doc_type}" if doc_type else "")
    print(f"\nScraping: {label}")

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
        print(f"  Page {page}: {len(records)} records (running total: {total})")

        if last_page is None or page >= last_page:
            break

        page += 1
        time.sleep(PAGE_DELAY)
        resp = session.get(f"{RESULTS_URL}?page={page}", timeout=SEARCH_TIMEOUT)
        resp.raise_for_status()

    median = drain_latency()
    if median is not None:
        # ~0.95s is the site's healthy baseline. A sustained climb means we are
        # asking for more than it wants to give, and the pacing above should
        # come back down.
        flag = "  <-- server slowing, consider easing pacing" if median > 2.0 else ""
        print(f"  Median detail fetch: {median:.2f}s{flag}")

    return total


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
    """(entity, status) combos a previous run of this dataset finished."""
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            return {tuple(pair) for pair in json.load(f)}
    except (OSError, ValueError):
        return set()  # unreadable or corrupt: treat as a fresh start


def save_progress(path, done):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(pair) for pair in done), f, indent=2)


def drop_unfinished_rows(csv_path, done):
    """Strip rows belonging to combos that were started but never finished.

    A run killed mid-combo leaves that combo's partial rows on disk with no
    record of how far it got. Resuming would re-scrape the combo from page 1
    and duplicate them, so the partial rows are discarded first. Combos in
    `done` are complete and keep their rows.
    """
    if not os.path.exists(csv_path):
        return 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return 0

    header, body = rows[0], rows[1:]
    # Entity Name is column 3, Status is column 8 -- see the header written in main().
    kept = [r for r in body if len(r) > 8 and (r[3], r[8]) in done]
    dropped = len(body) - len(kept)

    if dropped:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(kept)
    return dropped


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


def combos(entities):
    """(entity_name, entity_val, status) to scrape, biggest known agencies first.

    Ordering only decides what has landed if a run stops early -- the checkpoint
    makes the end result identical either way.
    """
    ordered = sorted(entities, key=lambda n: (STATE_PRIORITY.index(n) if n in STATE_PRIORITY else len(STATE_PRIORITY), n))
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
    args = parser.parse_args()

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
    progress_path = progress_file(args.dataset)

    if args.fresh:
        for p in (progress_path, output_csv):
            if os.path.exists(p):
                os.remove(p)
        print(f"--fresh: cleared {progress_path} and {output_csv}")

    done = load_progress(progress_path)
    all_combos = combos(entities)
    todo = [c for c in all_combos if (c[0], c[2]) not in done]

    if args.status:
        print(f"TOTAL={len(all_combos)}")
        print(f"DONE={len(all_combos) - len(todo)}")
        print(f"REMAINING={len(todo)}")
        return

    print(f"{len(todo):,} (entity, status) combos remaining of {len(all_combos):,}")

    dropped = drop_unfinished_rows(output_csv, done)
    if dropped:
        print(f"Discarded {dropped:,} partial rows from a combo that was interrupted mid-scrape.")

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

            grand_total += scrape_entity(
                session, entity_name, entity_val, status, entity_type, doc_type, writer
            )
            f.flush()
            # Only marked done once the combo's rows are all on disk, so an
            # interrupted combo is re-scraped rather than half-recorded.
            done.add((entity_name, status))
            save_progress(progress_path, done)

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
