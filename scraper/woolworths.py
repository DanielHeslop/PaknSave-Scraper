"""Entry point for the Woolworths NZ scrape, in three steps:

  1. Browse: every path in woolworths_categories.txt (the watch categories) is
     read through the public browse API.
  2. Search: the app's list for this store (GET ?store=woolworths) gives the terms;
     each is looked up through the public product-search API with a short delay
     between terms, time-boxed to about 20 minutes. Found / not-found is reported
     back to the app. A failure here never stops step 3.
  3. Publish: browse + matched search results, de-duplicated by product id, go to
     the mailbox. Runs even if step 2 failed.

robots.txt is fetched once per run; it disallows /shop/search (the HTML page) but
not /api/v1/products, which is all this uses. Plain HTTP, no browser.

Default mode is a DRY RUN: nothing is written, published or reported. Pass --save
to do it for real.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from scraper.extract import is_valid_product
from scraper.ingredients import DEFAULT_INGREDIENT_LIST_URL, fetch_store_lists
from scraper.matching import match_products
from scraper.publish import publish_to_mailbox
from scraper.robots import RobotsDisallowed
from scraper.search_step import (
    SEARCH_TIME_BUDGET_SECONDS,
    SearchOutcome,
    dedupe_products,
    log_outcome,
    plan_terms,
    report_outcome,
    summary_line,
)
from scraper.storage import update_price_history, write_snapshot
from scraper.woolworths_transport import BotDetected, browse_products, search_products

STORE = "woolworths"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TERMS_PATH = os.path.join(REPO_ROOT, "scraper", "woolworths_terms.txt")
DEFAULT_CATEGORIES_PATH = os.path.join(REPO_ROOT, "woolworths_categories.txt")
# Own snapshot directory: write_snapshot() replaces a whole <date>.json file, so
# sharing PAK'nSAVE's directory would clobber it. price_history.json is shared
# (keyed per product_id, append-only).
DEFAULT_SNAPSHOTS_DIR = os.path.join(REPO_ROOT, "data", "woolworths_snapshots")
DEFAULT_HISTORY_PATH = os.path.join(REPO_ROOT, "data", "price_history.json")
DEFAULT_BROWSE_MAX_PAGES = 5


def log(message: str) -> None:
    print(message, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Woolworths NZ price scraper (read-only, plain HTTP against the public search API)."
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Write a snapshot and update price_history.json. Default is a dry run (prints only).",
    )
    parser.add_argument(
        "--terms",
        default=DEFAULT_TERMS_PATH,
        help="Fallback term list, used only if the app's list can't be fetched.",
    )
    parser.add_argument("--categories", default=DEFAULT_CATEGORIES_PATH)
    parser.add_argument("--ingredient-list-url", default=DEFAULT_INGREDIENT_LIST_URL)
    parser.add_argument("--snapshots-dir", default=DEFAULT_SNAPSHOTS_DIR)
    parser.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)
    parser.add_argument(
        "--size",
        type=int,
        default=30,
        help="Max results to request per search term (default 30).",
    )
    return parser.parse_args(argv)


def parse_terms_file(path: str | Path) -> list[str]:
    """Read woolworths_terms.txt: one search term per non-comment, non-blank line."""
    path = Path(path)
    if not path.exists():
        return []
    terms = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(line)
    return terms


@dataclass(frozen=True)
class BrowsePage:
    path: str
    label: str
    covers: tuple[str, ...] = ()
    max_pages: int = DEFAULT_BROWSE_MAX_PAGES


def parse_categories_file(path: str | Path) -> list[BrowsePage]:
    """Read woolworths_categories.txt: `<dept>/<aisle>[/<shelf>] [pages=N] [category=x] [covers=a,b]`."""
    path = Path(path)
    if not path.exists():
        return []
    pages: list[BrowsePage] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        browse_path = parts[0].strip("/")
        label = browse_path.split("/")[-1]
        covers: tuple[str, ...] = ()
        max_pages = DEFAULT_BROWSE_MAX_PAGES
        for part in parts[1:]:
            if part.startswith("category="):
                label = part.split("=", 1)[1]
            elif part.startswith("covers="):
                covers = tuple(w for w in part.split("=", 1)[1].lower().split(",") if w)
            elif part.startswith("pages="):
                try:
                    max_pages = max(1, int(part.split("=", 1)[1]))
                except ValueError:
                    pass
        pages.append(BrowsePage(browse_path, label, covers, max_pages))
    return pages


def _keep_valid(products: list[dict], dropped: list[str]) -> list[dict]:
    kept = []
    for product in products:
        valid, reason = is_valid_product(product)
        if valid:
            kept.append(product)
        else:
            dropped.append(f"{product['product_id']} {product['name']} - {reason}")
    return kept


def browse_all(pages: list[BrowsePage]) -> dict:
    """Step 1: read every browse path. A failing path is logged and skipped."""
    products: list[dict] = []
    dropped: list[str] = []
    scraped: dict[str, tuple[str, ...]] = {}
    blocked: list[str] = []
    failed = 0
    for i, page in enumerate(pages, start=1):
        log(f"\n[{i}/{len(pages)}] browsing: {page.path}")
        try:
            found, skip_reasons, total = browse_products(page.path, page.max_pages, page.label)
        except RobotsDisallowed as e:
            log(f"  SKIPPED: robots.txt disallows {e}")
            failed += 1
            continue
        except BotDetected as e:
            log(f"  BLOCKED: {e}")
            blocked.append(f"{page.path} - {e}")
            failed += 1
            continue
        except Exception as e:
            log(f"  FAILED: {type(e).__name__}: {e}")
            failed += 1
            continue
        log(f"  {len(found)} of {total} product(s) read, {len(skip_reasons)} skipped")
        dropped.extend(f"[{page.path}] {r}" for r in skip_reasons)
        products.extend(_keep_valid(found, dropped))
        scraped.setdefault(page.label, page.covers)
    products = dedupe_products(products)
    stats = {
        "categories_scraped": len(scraped),
        "categories_failed": failed,
        "terms_blocked": blocked,
        "products_dropped": dropped,
    }
    return {"products": products, "scraped_categories": scraped, "stats": stats}


def search_all(
    terms: list[str], size: int, deadline: float | None = None
) -> tuple[SearchOutcome, list[str]]:
    """Step 2: look each term up through the product-search API.

    A term is found when at least one result shares a word with it (the API is
    fuzzy and returns loose matches). It is not-found only when the search ran
    and nothing sensible came back. A robots refusal or a failed request leaves
    the term unreported; a 403/429 stops the whole step.
    """
    outcome = SearchOutcome()
    blocked: list[str] = []
    for index, term in enumerate(terms):
        if deadline is not None and time.monotonic() > deadline:
            outcome.unsearched = terms[index:]
            log(f"Search step ran out of time - {len(outcome.unsearched)} term(s) not searched.")
            break
        try:
            results, _ = search_products(term, size=size)
        except RobotsDisallowed as e:
            log(f"  [{term}] SKIPPED: robots.txt disallows {e}")
            outcome.searched.append(term)
            outcome.unreported.append(term)
            continue
        except BotDetected as e:
            log(f"  [{term}] BLOCKED: {e} - stopping the search step.")
            blocked.append(f"{term} - {e}")
            outcome.unsearched = terms[index:]
            break
        except Exception as e:
            log(f"  [{term}] search failed ({type(e).__name__}: {e}) - not reported either way.")
            outcome.searched.append(term)
            outcome.unreported.append(term)
            continue
        outcome.searched.append(term)
        matches = match_products(term, [p for p in results if is_valid_product(p)[0]])
        if matches:
            outcome.found[term] = matches
        else:
            outcome.not_found.append(term)
    return outcome, blocked


def run_search_step(args) -> tuple[SearchOutcome, list[str]]:
    deadline = time.monotonic() + SEARCH_TIME_BUDGET_SECONDS
    lists = fetch_store_lists(STORE, args.ingredient_list_url)
    reportable = lists is not None
    skipped: list[str] = []
    if lists is None:
        terms = parse_terms_file(args.terms)
        log(f"App list unavailable - falling back to {len(terms)} term(s) from {args.terms}; nothing will be reported.")
    else:
        terms, skipped = plan_terms(lists)
        log(
            f"App list for {STORE}: {len(lists.search_terms)} search term(s), {len(lists.watch_terms)} watch "
            f"term(s), {len(skipped)} skipped."
        )
    log(f"{len(terms)} term(s) to search.")
    outcome, blocked = search_all(terms, args.size, deadline)
    outcome.skipped = skipped
    log_outcome(outcome)
    if not reportable:
        pass
    elif args.save:
        report_outcome(STORE, outcome)
    else:
        log("Dry run - would report to the app:")
        log(f"  found     : {json.dumps({'terms': list(outcome.found)})}")
        log(f"  not-found : {json.dumps({'terms': outcome.not_found, 'searchedAt': '<now>'})}")
    return outcome, blocked


def print_table(products: list[dict]) -> None:
    print()
    header = f"{'ID':>10} | {'Name':<40} | {'Size':<10} | {'Price':>7} | {'Unit Price'}"
    print(header)
    print("-" * len(header))
    for p in products:
        name = (p["name"] or "")[:40]
        size = p["size"] or ""
        price = f"${p['price']:.2f}" if p["price"] is not None else ""
        if p["unit_price"] is not None:
            unit_price = f"${p['unit_price']:.2f}/{p['unit']}"
        else:
            unit_price = ""
        print(f"{p['product_id']:>10} | {name:<40} | {size:<10} | {price:>7} | {unit_price}")
    print()


def print_summary(stats: dict) -> None:
    print("=" * 60)
    print("WOOLWORTHS RUN SUMMARY")
    print("=" * 60)
    print(f"Browse paths read           : {stats['categories_scraped']}")
    print(f"Browse paths failed         : {stats['categories_failed']}")
    print(f"Requests blocked (403/429)  : {len(stats['terms_blocked'])}")
    for reason in stats["terms_blocked"]:
        print(f"  - {reason}")
    print(f"Products saved              : {stats['products_saved']}")
    print(f"  - with a unit price       : {stats['products_with_unit_price']}")
    print(f"  - without a unit price    : {stats['products_without_unit_price']}")
    print(f"Products dropped entirely   : {len(stats['products_dropped'])}")
    for reason in stats["products_dropped"]:
        print(f"  - {reason}")
    print("=" * 60)


def main() -> int:
    started = time.monotonic()
    args = parse_args()
    if not args.save:
        log("(Dry Run Mode - nothing is written, published or reported. Pass --save to do it for real.)")

    log("\nSTEP 1: BROWSE (watch categories)")
    try:
        browse = browse_all(parse_categories_file(args.categories))
    except Exception as e:
        log(f"UNEXPECTED ERROR - stopping run: {e!r}")
        return 1
    browse_products_list = browse["products"]
    stats = browse["stats"]

    if args.save and browse_products_list:
        today = date.today().isoformat()
        snapshot_path = write_snapshot(browse_products_list, args.snapshots_dir, today)
        _, new_entries = update_price_history(browse_products_list, args.history_path)
        log(f"Snapshot written to {snapshot_path}")
        log(f"Price history updated: {new_entries} new entr{'y' if new_entries == 1 else 'ies'} added")

    log("\nSTEP 2: SEARCH (product-search API)")
    outcome = SearchOutcome()
    try:
        outcome, blocked = run_search_step(args)
        stats["terms_blocked"] = stats["terms_blocked"] + blocked
    except Exception as e:
        log(f"Search step failed ({type(e).__name__}: {e}) - carrying on to publish.")

    to_publish = dedupe_products(browse_products_list + outcome.matched_products)
    stats["products_saved"] = len(to_publish)
    stats["products_with_unit_price"] = sum(1 for p in to_publish if p["unit_price"] is not None)
    stats["products_without_unit_price"] = len(to_publish) - stats["products_with_unit_price"]

    log("\nSTEP 3: PUBLISH")
    print_table(to_publish)
    print_summary(stats)
    published = 0
    if args.save:
        extra = [p for p in outcome.matched_products if p not in browse_products_list]
        if extra:
            try:
                today = date.today().isoformat()
                write_snapshot(to_publish, args.snapshots_dir, today)
                update_price_history(extra, args.history_path)
            except Exception as e:
                log(f"Could not update snapshot/history with search results ({type(e).__name__}: {e}).")
        try:
            published = publish_to_mailbox(to_publish) or 0
        except Exception as e:
            log(f"Publishing failed ({type(e).__name__}: {e}).")
    else:
        log("Dry run - nothing published.")

    print(f"Ingredients matched         : {len(outcome.found)}")
    log(summary_line(published, outcome, stats["categories_scraped"], time.monotonic() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
