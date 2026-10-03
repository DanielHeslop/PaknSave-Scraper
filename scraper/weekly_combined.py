"""Combined weekly job: category scrape + search-driven ingredient lookup,
in one run, skipping redundant by-name searches.

Runs scraper.run's category scrape first, then scraper.search_run's
ingredient search over the recipe app's ingredient list - but before
searching by name for an ingredient, checks it against this same run's
just-scraped category products using scraper.matching.is_confident_match
(the same "shares at least one meaningful word" check search_run.py itself
uses for its own results). An ingredient with a confident match there is
treated as already covered and its by-name search is skipped entirely.

This replaces running scraper.run and scraper.search_run as two separate
cron jobs (one daily, one weekly) with a single weekly job. Both underlying
modules are unchanged and still work exactly as before when invoked on
their own (`python -m scraper.run --save`, `python -m scraper.search_run
--save`) - this module only composes their existing functions.

Default mode is a DRY RUN, same convention as both underlying scripts:
prints tables and summaries, writes nothing to disk (no snapshots, no price
history, no search_cache.json update, no mailbox publish). Pass --save to
write results for real.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from scraper.ingredients import DEFAULT_INGREDIENT_LIST_URL, IngredientListError, fetch_ingredient_list
from scraper.matching import is_confident_match, matching_words
from scraper.publish import publish_to_mailbox
from scraper.run import (
    DEFAULT_CATEGORIES_PATH,
    DEFAULT_HISTORY_PATH,
    DEFAULT_OVERRIDES_PATH,
    DEFAULT_SNAPSHOTS_DIR,
)
from scraper.run import print_summary as print_category_summary
from scraper.run import print_table as print_category_table
from scraper.run import scrape_all
from scraper.search_cache import DEFAULT_STALE_AFTER_DAYS, load_search_cache, save_search_cache
from scraper.search_run import DEFAULT_CACHE_PATH, DEFAULT_SEARCH_SNAPSHOTS_DIR
from scraper.search_run import print_summary as print_search_summary
from scraper.search_run import print_table as print_search_table
from scraper.search_run import search_all
from scraper.storage import update_price_history, write_snapshot


def log(message: str) -> None:
    print(message, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Combined weekly run: category scrape + ingredient search, "
        "skipping by-name search for ingredients already covered by the category scrape."
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Write snapshots, price history, search_cache.json, and publish. "
        "Default is a dry run (prints only, writes nothing).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window instead of running headless. Useful for debugging.",
    )
    parser.add_argument("--categories", default=DEFAULT_CATEGORIES_PATH)
    parser.add_argument("--overrides", default=DEFAULT_OVERRIDES_PATH)
    parser.add_argument("--snapshots-dir", default=DEFAULT_SNAPSHOTS_DIR)
    parser.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--ingredient-list-url", default=DEFAULT_INGREDIENT_LIST_URL)
    parser.add_argument("--search-snapshots-dir", default=DEFAULT_SEARCH_SNAPSHOTS_DIR)
    parser.add_argument("--cache-path", default=DEFAULT_CACHE_PATH)
    parser.add_argument(
        "--stale-days",
        type=int,
        default=DEFAULT_STALE_AFTER_DAYS,
        help="Skip re-searching an ingredient found within this many days (default 7).",
    )
    parser.add_argument(
        "--publish-snapshot",
        metavar="PATH",
        default=None,
        help="Publish an already-written category snapshot (e.g. "
        "data/snapshots/2026-10-03.json) to the mailbox without re-scraping, then exit - "
        "for recovering a run where Part 1's snapshot/history were already written but "
        "the job failed before publishing (e.g. a Part 2 ingredient-list fetch failure). "
        "Runs the same publish path and scrape-exclusions filtering as a normal --save "
        "run. Dry run by default (reports counts only, posts nothing); pass --save too "
        "to actually publish.",
    )
    return parser.parse_args(argv)


def publish_snapshot_file(path: str, save: bool) -> int:
    """Load a previously-written snapshot JSON file and publish it to the
    mailbox (through the same publish_to_mailbox path - and so the same
    scrape-exclusions filtering - as a normal run), without re-scraping.

    Dry run by default: reports counts only, never POSTs. Pass save=True to
    actually publish.
    """
    snapshot_path = Path(path)
    try:
        raw = snapshot_path.read_text()
    except FileNotFoundError:
        log(f"Snapshot not found: {snapshot_path}")
        return 1

    try:
        products = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"Snapshot at {snapshot_path} is not valid JSON: {e}")
        return 1

    if not isinstance(products, list):
        log(f"Snapshot at {snapshot_path} is not a JSON array of products.")
        return 1

    log(f"Loaded {len(products)} product(s) from {snapshot_path}")
    publish_to_mailbox(products, dry_run=not save)
    return 0


def split_covered_ingredients(
    ingredient_names: list[str], category_products: list[dict]
) -> tuple[list[tuple[str, dict]], list[str]]:
    """Split ingredient names into (already covered, still need searching).

    "Covered" means a confident match (scraper.matching.is_confident_match)
    was found among this run's category-scraped products - the same check
    scraper.search_run.py itself uses to judge its own search results, so
    an ingredient is never held to a stricter or looser standard depending
    on which path found it.
    """
    covered: list[tuple[str, dict]] = []
    remaining: list[str] = []
    for name in ingredient_names:
        match = next(
            (p for p in category_products if is_confident_match(name, p["name"])),
            None,
        )
        if match is not None:
            covered.append((name, match))
        else:
            remaining.append(name)
    return covered, remaining


async def main_async(args: argparse.Namespace) -> int:
    if not args.save:
        log("(Dry Run Mode - nothing will be written to disk. Pass --save to write results.)")

    log("\n" + "=" * 60)
    log("PART 1: CATEGORY SCRAPE")
    log("=" * 60)
    try:
        category_result = await scrape_all(args.categories, args.overrides, args.headed)
    except Exception as e:
        log(f"UNEXPECTED ERROR during category scrape - stopping run: {e!r}")
        return 1

    category_products = category_result["products"]
    print_category_table(category_products)
    print_category_summary(category_result["stats"])

    if args.save:
        today = date.today().isoformat()
        snapshot_path = write_snapshot(category_products, args.snapshots_dir, today)
        _, new_entries = update_price_history(category_products, args.history_path)
        log(f"Snapshot written to {snapshot_path}")
        log(f"Price history updated: {new_entries} new entr{'y' if new_entries == 1 else 'ies'} added")

        # Published here, before Part 2 runs, so a Part 2 failure (e.g. the
        # ingredient-list fetch) can never cost Part 1's results - see
        # weekly_combined.py's module docstring history / the 2026-09-27 and
        # 2026-10-03 incidents where Part 2's 401 caused the whole run to
        # exit before this ever ran. Safe to call again with the same
        # products if Part 2 later adds its own publish_to_mailbox call too:
        # the mailbox key is "supermarket|product_id", so republishing an
        # unchanged item is just an overwrite with identical data.
        publish_to_mailbox(category_products)

    log("\n" + "=" * 60)
    log("PART 2: INGREDIENT SEARCH (skipping ingredients already covered above)")
    log("=" * 60)
    try:
        ingredient_names = fetch_ingredient_list(args.ingredient_list_url)
    except IngredientListError as e:
        log(f"Could not fetch the ingredient list - stopping run: {e}")
        return 1

    if not ingredient_names:
        log("Ingredient list was empty - nothing to search.")
        return 0

    covered, remaining = split_covered_ingredients(ingredient_names, category_products)
    for name, product in covered:
        words = ", ".join(sorted(matching_words(name, product["name"])))
        log(
            f"  {name}: matched from {product['category']} category scrape, search skipped "
            f"(matched {product['product_id']} {product['name']!r} on: {words})"
        )
    log(
        f"{len(covered)} ingredient(s) already covered by the category scrape; "
        f"{len(remaining)} remaining for by-name search."
    )

    cache = load_search_cache(args.cache_path)

    try:
        search_result = await search_all(remaining, cache, args.overrides, args.headed, args.stale_days)
    except Exception as e:
        log(f"UNEXPECTED ERROR during ingredient search - stopping run: {e!r}")
        return 1

    search_products = search_result["products"]
    print_search_table(search_products)
    print_search_summary(search_result["stats"])
    log(f"Ingredients covered by category scrape (search skipped): {len(covered)}")

    if args.save:
        today_iso = date.today().isoformat()
        if search_products:
            snapshot_path = write_snapshot(search_products, args.search_snapshots_dir, today_iso)
            _, new_entries = update_price_history(search_products, args.history_path)
            log(f"Search snapshot written to {snapshot_path}")
            log(f"Price history updated: {new_entries} new entr{'y' if new_entries == 1 else 'ies'} added")
        else:
            log("No matched search products this run - nothing to snapshot or add to price history.")

        for name in search_result["newly_found"]:
            cache[name] = today_iso
        save_search_cache(cache, args.cache_path)
        log(f"search_cache.json updated: {len(search_result['newly_found'])} ingredient(s) marked found today.")

        # Only the search results here - category_products was already
        # published right after Part 1 (above), before Part 2 ever ran, so
        # a Part 2 failure can't lose them. Re-sending category_products
        # again here would be harmless (mailbox key is
        # "supermarket|product_id", so a repeat is just an overwrite with
        # identical data) but pointless, since it was just published.
        if search_products:
            publish_to_mailbox(search_products)

    return 0


def main() -> None:
    args = parse_args()
    if args.publish_snapshot:
        sys.exit(publish_snapshot_file(args.publish_snapshot, args.save))
    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
