"""Search-driven scrape mode: look up specific recipe ingredients by name,
rather than only reading fixed category pages.

Separate from and additional to scraper/run.py's daily category scrape -
see README.md's "Weekly ingredient search" section for how the two modes
differ and when each runs.

Default mode is a DRY RUN, same convention as scraper/run.py: prints a
table, writes nothing to disk (no snapshot, no price history update, no
search_cache.json update, no publish). Pass --save to write results for
real. This means a dry run never affects which ingredients count as
"recently found" - only a --save run advances that clock.

Error handling hierarchy (mirrors scraper/run.py's, adapted for search):
  1. A single field unreadable on the matched result -> keep the product,
     leave that field None. Normal, not an error.
  2. The top result's price/ID/name unreadable, or it fails the same
     sanity checks the category scraper uses -> drop only this ingredient,
     log why, continue to the next ingredient.
  3. No results, or the top result shares no obvious wording with what was
     searched -> "no confident match", logged plainly. Also normal, not an
     error - see README.md.
  4. A whole search page fails to load after retries -> log it, skip to
     the next ingredient, continue the run.
  5. A genuinely unexpected error -> log full detail, stop the run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import urllib.parse
from datetime import date

from scraper.browser import (
    PageLoadTimeout,
    navigate_and_wait_ready,
    new_pinned_context,
    stealth_playwright,
)
from scraper.extract import extract_product, find_product_tiles, is_valid_product
from scraper.ingredients import DEFAULT_INGREDIENT_LIST_URL, IngredientListError, fetch_ingredient_list
from scraper.matching import is_confident_match, matching_words
from scraper.overrides import load_overrides
from scraper.publish import publish_to_mailbox
from scraper.run import apply_override
from scraper.search_cache import (
    DEFAULT_STALE_AFTER_DAYS,
    load_search_cache,
    save_search_cache,
    was_recently_found,
)
from scraper.storage import update_price_history, write_snapshot

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OVERRIDES_PATH = os.path.join(REPO_ROOT, "overrides.txt")
DEFAULT_SEARCH_SNAPSHOTS_DIR = os.path.join(REPO_ROOT, "data", "search_snapshots")
DEFAULT_HISTORY_PATH = os.path.join(REPO_ROOT, "data", "price_history.json")
DEFAULT_CACHE_PATH = os.path.join(REPO_ROOT, "data", "search_cache.json")

SEARCH_CATEGORY_LABEL = "search"


def log(message: str) -> None:
    print(message, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PAK'nSAVE search-driven ingredient price lookup (read-only)."
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Write results and update search_cache.json / price_history.json. "
        "Default is a dry run (prints only, nothing recorded as 'found').",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window instead of running headless. Useful for debugging.",
    )
    parser.add_argument("--ingredient-list-url", default=DEFAULT_INGREDIENT_LIST_URL)
    parser.add_argument("--overrides", default=DEFAULT_OVERRIDES_PATH)
    parser.add_argument("--snapshots-dir", default=DEFAULT_SEARCH_SNAPSHOTS_DIR)
    parser.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--cache-path", default=DEFAULT_CACHE_PATH)
    parser.add_argument(
        "--stale-days",
        type=int,
        default=DEFAULT_STALE_AFTER_DAYS,
        help="Skip re-searching an ingredient found within this many days (default 7).",
    )
    return parser.parse_args(argv)


def build_search_url(ingredient_name: str) -> str:
    """The search URL/mechanism confirmed live (search-mode Step 0, via
    playwright-stealth, 2026-09-05): PAK'nSAVE's own search box submits to
    /shop/search?q=<term>&sf=shopping - confirmed to also work when
    navigated to directly, with no need to interact with the search box
    itself. Confirmed against a real search ("milk"): the results page uses
    the exact same data-testid structure as category pages (tiles matching
    -EA-000/-KGM-000, plus price-dollars/price-cents/product-subtitle), so
    scraper/extract.py's existing extraction code applies unchanged.
    """
    query = urllib.parse.quote_plus(ingredient_name)
    return f"https://www.paknsave.co.nz/shop/search?q={query}&sf=shopping"


async def search_one_ingredient(
    page, ingredient_name: str, scraped_at: str, overrides: dict
) -> tuple[dict | None, str | None]:
    """Search for one ingredient and return (product, skip_reason).

    Exactly one of the two is set: a matched, validated product dict, or a
    plain-language reason nothing usable came of this search.
    """
    url = build_search_url(ingredient_name)
    try:
        await navigate_and_wait_ready(page, url, log)
    except PageLoadTimeout as e:
        return None, f"search page failed to load - {e}"

    tiles = await find_product_tiles(page)
    if not tiles:
        return None, "no confident match - no results returned"

    top_tile = tiles[0]
    result = await extract_product(top_tile, SEARCH_CATEGORY_LABEL, scraped_at)
    if result.drop_reason:
        return None, f"could not read top result - {result.drop_reason}"

    product = result.product
    if not is_confident_match(ingredient_name, product["name"]):
        return None, f"no confident match - top result was {product['name']!r}, no shared wording"

    override = overrides.get(product["product_id"])
    if override is not None:
        if override.skip:
            return None, f"{product['product_id']} {product['name']} - excluded via overrides.txt"
        product = apply_override(product, override)

    valid, reason = is_valid_product(product)
    if not valid:
        return None, f"{product['product_id']} {product['name']} - {reason}"

    product["ingredient"] = ingredient_name
    return product, None


async def search_all(
    ingredient_names: list[str],
    cache: dict[str, str],
    overrides_path: str,
    headed: bool,
    stale_days: int,
) -> dict:
    """Search every ingredient not skipped by the freshness cache, and
    return a results dict with products, skipped-as-fresh names, and stats.
    """
    overrides = load_overrides(overrides_path)
    today = date.today()
    scraped_at = today.isoformat()

    to_search = [name for name in ingredient_names if not was_recently_found(name, cache, today, stale_days)]
    already_fresh = [name for name in ingredient_names if name not in to_search]

    log(f"{len(ingredient_names)} ingredient(s) total; {len(already_fresh)} already fresh "
        f"(found within {stale_days} days), {len(to_search)} to search.")

    products: list[dict] = []
    no_match: list[str] = []
    newly_found: list[str] = []

    async with stealth_playwright() as p:
        launch_kwargs = {"headless": not headed}
        chromium_override = os.environ.get("PWSCRAPER_CHROMIUM_PATH")
        if chromium_override:
            launch_kwargs["executable_path"] = chromium_override
        browser = await p.chromium.launch(**launch_kwargs)
        context = await new_pinned_context(browser)
        page = await context.new_page()

        for i, ingredient_name in enumerate(to_search, start=1):
            log(f"\n[{i}/{len(to_search)}] searching: {ingredient_name!r}")
            product, skip_reason = await search_one_ingredient(page, ingredient_name, scraped_at, overrides)

            if skip_reason is not None:
                log(f"  SKIPPED: {skip_reason}")
                no_match.append(f"{ingredient_name} - {skip_reason}")
                continue

            words = ", ".join(sorted(matching_words(ingredient_name, product["name"])))
            log(f"  MATCHED: {product['product_id']} {product['name']!r} (shared word(s): {words})")
            products.append(product)
            newly_found.append(ingredient_name)

        await browser.close()

    stats = {
        "ingredients_total": len(ingredient_names),
        "ingredients_already_fresh": already_fresh,
        "ingredients_searched": len(to_search),
        "ingredients_matched": len(products),
        "ingredients_no_match": no_match,
    }
    return {"products": products, "newly_found": newly_found, "stats": stats}


def print_table(products: list[dict]) -> None:
    print()
    header = f"{'Ingredient':<30} | {'ID':>10} | {'Product Name':<35} | {'Price':>7} | {'Unit Price'}"
    print(header)
    print("-" * len(header))
    for p in products:
        ingredient = (p.get("ingredient") or "")[:30]
        name = (p["name"] or "")[:35]
        price = f"${p['price']:.2f}" if p["price"] is not None else ""
        if p["unit_price"] is not None:
            unit_price = f"${p['unit_price']:.2f}/{p['unit']}"
        else:
            unit_price = ""
        print(f"{ingredient:<30} | {p['product_id']:>10} | {name:<35} | {price:>7} | {unit_price}")
    print()


def print_summary(stats: dict) -> None:
    print("=" * 60)
    print("SEARCH RUN SUMMARY")
    print("=" * 60)
    print(f"Ingredients total          : {stats['ingredients_total']}")
    print(f"  - already fresh (skipped): {len(stats['ingredients_already_fresh'])}")
    print(f"  - searched this run      : {stats['ingredients_searched']}")
    print(f"Ingredients matched        : {stats['ingredients_matched']}")
    print(f"Ingredients with no match  : {len(stats['ingredients_no_match'])} "
          "(expected for imports, homemade items, things PAK'nSAVE doesn't sell - see README.md)")
    for reason in stats["ingredients_no_match"]:
        print(f"  - {reason}")
    print("=" * 60)


async def main_async(args: argparse.Namespace) -> int:
    if not args.save:
        log("(Dry Run Mode - nothing will be written to disk, and nothing counts as 'found'. Pass --save to write results.)")

    try:
        ingredient_names = fetch_ingredient_list(args.ingredient_list_url)
    except IngredientListError as e:
        log(f"Could not fetch the ingredient list - stopping run: {e}")
        return 1

    if not ingredient_names:
        log("Ingredient list was empty - nothing to search.")
        return 0

    cache = load_search_cache(args.cache_path)

    try:
        result = await search_all(ingredient_names, cache, args.overrides, args.headed, args.stale_days)
    except Exception as e:
        log(f"UNEXPECTED ERROR - stopping run: {e!r}")
        return 1

    products = result["products"]
    stats = result["stats"]

    print_table(products)
    print_summary(stats)

    if args.save:
        today_iso = date.today().isoformat()
        if products:
            snapshot_path = write_snapshot(products, args.snapshots_dir, today_iso)
            _, new_entries = update_price_history(products, args.history_path)
            log(f"Snapshot written to {snapshot_path}")
            log(f"Price history updated: {new_entries} new entr{'y' if new_entries == 1 else 'ies'} added")
        else:
            log("No matched products this run - nothing to snapshot or add to price history.")

        for name in result["newly_found"]:
            cache[name] = today_iso
        save_search_cache(cache, args.cache_path)
        log(f"search_cache.json updated: {len(result['newly_found'])} ingredient(s) marked found today.")

        # Best-effort only, same publish path the category scraper uses -
        # see publish.py's docstring.
        publish_to_mailbox(products)

    return 0


def main() -> None:
    args = parse_args()
    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
