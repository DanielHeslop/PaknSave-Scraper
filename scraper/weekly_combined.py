"""Weekly Foodstuffs job (Pak'nSave or New World): category scrape, then a local
search step, then publish - in that order.

  1. Category scrape: every page in the site's categories file (including the
     watch categories), with robots.txt honoured and a polite pause between
     page loads. Snapshot and price history are written straight away.
  2. Search step: the app's per-store list (GET ?store=<store>) gives the
     terms to look for. They are matched against the products just scraped
     by the app's own matcher (POST /api/match-terms), with the local matcher
     as the backup if that call fails - the sites' own search pages are
     disallowed by robots.txt, so nothing is requested from them. Found /
     not-found is reported back to the app. Time-boxed; a failure here is
     logged and never stops step 3.
  3. Publish: the de-duplicated products are posted to the app as one run
     (POST /api/price-runs, in parts - scraper/price_runs.py). Runs even if
     step 2 failed. --legacy-mailbox posts to the old mailbox instead.

Default mode is a DRY RUN: nothing is written, published or reported (the
would-be reports are printed instead). Pass --save to do it for real.
`python -m scraper.search_run` runs this same job.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

from scraper.category_mapping import MappingResult, extra_foodstuffs_pages, sync_and_map
from scraper.ingredients import DEFAULT_INGREDIENT_LIST_URL, fetch_store_lists
from scraper.price_runs import now_nz_iso, post_snapshot_file, publish_run, write_scrape_meta
from scraper.browser import SITES, parse_categories_file
from scraper.robots import RobotsGuard
from scraper.run import DEFAULT_HISTORY_PATH, DEFAULT_OVERRIDES_PATH, resolve_site_defaults, scrape_all
from scraper.run import print_summary as print_category_summary
from scraper.run import print_table as print_category_table
from scraper.search_step import (
    SEARCH_TIME_BUDGET_SECONDS,
    SearchOutcome,
    category_words,
    dedupe_products,
    log_outcome,
    plan_terms,
    report_outcome,
    search_products_for_terms,
    summary_line,
)
from scraper.storage import update_price_history, write_snapshot


def log(message: str) -> None:
    print(message, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Weekly Foodstuffs run: category scrape, local search over the "
        "scraped products, then publish."
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Write snapshots and price history, publish, and report found/not-found "
        "to the app. Default is a dry run (prints only, writes and sends nothing).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window instead of running headless. Useful for debugging.",
    )
    parser.add_argument(
        "--site",
        choices=sorted(SITES),
        default="paknsave",
        help="Which Foodstuffs chain to run (default: paknsave).",
    )
    parser.add_argument("--categories", default=None)
    parser.add_argument("--overrides", default=DEFAULT_OVERRIDES_PATH)
    parser.add_argument("--snapshots-dir", default=None)
    parser.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--ingredient-list-url", default=DEFAULT_INGREDIENT_LIST_URL)
    parser.add_argument(
        "--legacy-mailbox",
        action="store_true",
        help="Post to the old mailbox instead of /api/price-runs (fallback only).",
    )
    parser.add_argument(
        "--publish-snapshot",
        metavar="PATH",
        default=None,
        help="Post an already-written category snapshot (e.g. "
        "data/snapshots/2026-10-03.json) to the app as a run without re-scraping, then exit - "
        "for recovering a run where Part 1's snapshot/history were already written but "
        "the job failed before publishing (e.g. a Part 2 ingredient-list fetch failure). "
        "Runs the same publish path and scrape-exclusions filtering as a normal --save "
        "run. Dry run by default (reports counts only, posts nothing); pass --save too "
        "to actually publish.",
    )
    return resolve_site_defaults(parser.parse_args(argv))


def publish_snapshot_file(path: str, save: bool, store: str = "paknsave", legacy: bool = False) -> int:
    """Post a previously-written snapshot JSON file as a run (the same path, run id and
    scrape-exclusions filtering as a normal run), without re-scraping.

    Dry run by default: shows the run's id, parts and sizes, posts nothing. Pass
    save=True to actually post.
    """
    result = post_snapshot_file(path, store, dry_run=not save, legacy=legacy)
    if result is None:
        return 1
    if save:
        log(f"SUMMARY: {result.summary() or 'nothing posted'}")
    return 0


def _empty_scrape_result() -> dict:
    return {"products": [], "stats": {"categories_scraped": 0}, "scraped_categories": {}}


async def main_async(args: argparse.Namespace) -> int:
    started = time.monotonic()
    store = args.site
    if not args.save:
        log("(Dry Run Mode - nothing is written, published or reported. Pass --save to do it for real.)")

    guard = RobotsGuard()

    log("\n" + "=" * 60)
    log("STEP 0: CATEGORY TREE AND WATCH CATEGORIES")
    log("=" * 60)
    lists = _FETCH_LATER
    try:
        lists = fetch_store_lists(store, args.ingredient_list_url)
    except Exception as e:
        log(f"Could not fetch the app's list up front ({type(e).__name__}) - the search step will try again.")
    mapping: MappingResult | None = None
    extra_pages = []
    try:
        mapping = sync_and_map(store, None if lists is _FETCH_LATER else lists, save=args.save, guard=guard)
        if mapping:
            extra_pages = extra_foodstuffs_pages(store, mapping.mapped, parse_categories_file(args.categories))
            log(f"{len(extra_pages)} mapped watch categor(ies) to scrape beyond the categories file.")
    except Exception as e:
        log(f"Watch category step failed ({type(e).__name__}: {e}) - carrying on without it.")

    log("\n" + "=" * 60)
    log("STEP 1: CATEGORY SCRAPE")
    log("=" * 60)
    try:
        category_result = await scrape_all(
            args.categories, args.overrides, args.headed, args.site, guard, extra_pages=extra_pages
        )
    except Exception as e:
        log(f"UNEXPECTED ERROR during category scrape - stopping run: {e!r}")
        return 1

    category_products = category_result["products"]
    print_category_table(category_products)
    print_category_summary(category_result["stats"])
    scraped_categories = category_result.get("scraped_categories", {})

    scraped_at = category_result.get("scraped_at") or now_nz_iso()
    snapshot_path = None
    if args.save:
        today = date.today().isoformat()
        snapshot_path = write_snapshot(dedupe_products(category_products), args.snapshots_dir, today)
        write_scrape_meta(snapshot_path, scraped_at)
        _, new_entries = update_price_history(category_products, args.history_path)
        log(f"Snapshot written to {snapshot_path}")
        log(f"Price history updated: {new_entries} new entr{'y' if new_entries == 1 else 'ies'} added")

    log("\n" + "=" * 60)
    log("STEP 2: SEARCH (app matcher, local backup - matched against the products just scraped)")
    log("=" * 60)
    outcome = SearchOutcome()
    try:
        outcome = run_search_step(args, store, category_products, scraped_categories, lists)
    except Exception as e:
        log(f"Search step failed ({type(e).__name__}: {e}) - carrying on to publish.")

    log("\n" + "=" * 60)
    log("STEP 3: PUBLISH")
    log("=" * 60)
    to_publish = dedupe_products(category_products + outcome.matched_products)
    log(f"{len(to_publish)} unique product(s) after de-duplicating by product id.")
    published = 0
    run_status = None
    if args.save:
        try:
            run = publish_run(
                to_publish, store, scraped_at, legacy=args.legacy_mailbox, snapshot_path=snapshot_path
            )
            published = run.items
            run_status = run.summary()
        except Exception as e:
            log(f"Publishing failed ({type(e).__name__}: {e}).")
    else:
        log("Dry run - nothing published.")

    print(f"Ingredients matched         : {len(outcome.found)}")
    log(
        summary_line(
            published,
            outcome,
            len(scraped_categories),
            time.monotonic() - started,
            mapping.counts() if mapping else None,
            run_status,
        )
    )
    return 0


_FETCH_LATER = object()


def run_search_step(
    args, store: str, products: list[dict], scraped_categories: dict, lists=_FETCH_LATER
) -> SearchOutcome:
    """Match the app's list for this store (app matcher, local backup) and report the result.

    `lists` is the list already fetched at the start of the run (None if the app
    couldn't be reached); by default it is fetched here.
    """
    deadline = time.monotonic() + SEARCH_TIME_BUDGET_SECONDS
    if lists is _FETCH_LATER:
        lists = fetch_store_lists(store, args.ingredient_list_url)
    if lists is None:
        return SearchOutcome()

    terms, skipped = plan_terms(lists)
    log(
        f"App list for {store}: {len(lists.search_terms)} search term(s), {len(lists.watch_terms)} watch term(s), "
        f"{len(lists.watch_categories)} watch categor(ies) {lists.watch_categories} (scraped via the categories file, plus the ones mapped at this store), "
        f"{len(skipped)} skipped."
    )
    scraped_words: set[str] = set()
    for label, covers in scraped_categories.items():
        scraped_words |= category_words(label, covers)

    outcome = search_products_for_terms(store, terms, products, scraped_words, deadline)
    outcome.skipped = skipped
    log_outcome(outcome)

    if args.save:
        report_outcome(store, outcome)
    else:
        log("Dry run - would report to the app:")
        log(f"  found     : {json.dumps({'terms': list(outcome.found)})}")
        log(f"  not-found : {json.dumps({'terms': outcome.not_found, 'searchedAt': '<now>'})}")
    return outcome


def main() -> None:
    args = parse_args()
    if args.publish_snapshot:
        sys.exit(publish_snapshot_file(args.publish_snapshot, args.save, args.site, args.legacy_mailbox))
    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
