"""Main entry point: visit categories.txt's pages, extract products, report.

Default mode is a DRY RUN: prints a table, writes nothing to disk. Pass
--save to write a snapshot and update the price history file. This mirrors
the reference project's `dotnet run` vs `dotnet run db` distinction, so
testing changes never risks the real data file.

Error handling hierarchy (see README.md for the plain-language version):
  1. A single field unreadable (e.g. no unit price) -> keep the product,
     leave that field None. Normal, not an error.
  2. Price/ID/name unreadable, or basic sanity checks fail -> drop only
     this product, log why, continue to the next product.
  3. A whole page fails to load after retries -> log it, skip to the next
     line in categories.txt, continue the run.
  4. A genuinely unexpected error -> log full detail, stop the run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import asdict
from datetime import date

from scraper.browser import (
    SITES,
    CategoryPage,
    PageLoadTimeout,
    navigate_and_wait_ready,
    new_pinned_context,
    parse_categories_file,
    polite_pause,
    stealth_playwright,
)
from scraper.extract import ExtractResult, extract_product, find_product_tiles, is_valid_product
from scraper import checkpoint as checkpoint_module
from scraper import cooking_wine, page_limits
from scraper.overrides import get_override, load_overrides
from scraper.page_limits import PageStat
from scraper.price_runs import now_nz_iso, publish_run, write_scrape_meta
from scraper.robots import RobotsGuard
from scraper.storage import update_price_history, write_snapshot
from scraper.store_category import foodstuffs_path_of
from scraper.unit_price import derive_from_size_and_price

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SITE = "paknsave"
DEFAULT_CATEGORIES_PATH = os.path.join(REPO_ROOT, "categories.txt")
DEFAULT_OVERRIDES_PATH = os.path.join(REPO_ROOT, "overrides.txt")
DEFAULT_SNAPSHOTS_DIR = os.path.join(REPO_ROOT, "data", "snapshots")
DEFAULT_HISTORY_PATH = os.path.join(REPO_ROOT, "data", "price_history.json")


def log(message: str) -> None:
    print(message, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Foodstuffs (PAK'nSAVE / New World) price-tracking scraper (read-only)."
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Write a snapshot and update price_history.json. Default is a dry run (prints only).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window instead of running headless. Useful for debugging.",
    )
    parser.add_argument(
        "--site",
        choices=sorted(SITES),
        default=DEFAULT_SITE,
        help="Which supermarket chain to scrape (default: paknsave). Same platform, "
        "different domain/store/categories file - see scraper/browser.py's SITES.",
    )
    # --categories and --snapshots-dir default to None here so their
    # site-specific defaults (categories.txt/data/snapshots for paknsave,
    # newworld_categories.txt/data/newworld_snapshots for newworld) can be
    # resolved after --site is known, in resolve_site_defaults() below -
    # an explicit flag value always overrides the site's default.
    parser.add_argument("--categories", default=None)
    parser.add_argument("--overrides", default=DEFAULT_OVERRIDES_PATH)
    parser.add_argument("--snapshots-dir", default=None)
    parser.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)
    parser.add_argument(
        "--legacy-mailbox",
        action="store_true",
        help="Post to the old mailbox instead of /api/price-runs (fallback only).",
    )
    args = parser.parse_args(argv)
    return resolve_site_defaults(args)


def resolve_site_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in --categories/--snapshots-dir from the chosen --site's
    defaults when the caller didn't pass an explicit value.

    For the default site (paknsave) this always resolves to exactly
    DEFAULT_CATEGORIES_PATH / DEFAULT_SNAPSHOTS_DIR - the same paths used
    before --site existed - so the no-flags invocation is unchanged.
    """
    site = SITES[args.site]
    if args.categories is None:
        args.categories = os.path.join(REPO_ROOT, site.default_categories_file)
    if args.snapshots_dir is None:
        args.snapshots_dir = os.path.join(REPO_ROOT, "data", site.default_snapshots_subdir)
    return args


def apply_override(product: dict, override) -> dict:
    """Apply a size (and recalculated unit price) or skip override in place."""
    if override.size:
        product["size"] = override.size
        derived = derive_from_size_and_price(override.size, product["price"])
        if derived is not None:
            product["unit_price"] = derived.amount
            product["unit"] = derived.unit
    return product


async def scrape_all(
    categories_path: str,
    overrides_path: str,
    headed: bool,
    site: str = DEFAULT_SITE,
    guard: RobotsGuard | None = None,
    extra_pages: list[CategoryPage] | None = None,
    checkpoint: bool = False,
) -> dict:
    """Run the full scrape and return a results dict with products and stats.

    `extra_pages` are scraped after the categories file's pages (the caller has
    already left out any the file lists) - used for the mapped watch categories.

    `checkpoint` (real runs only) saves progress after every finished category and picks up
    today's unfinished scrape of the same list, see scraper/checkpoint.py.

    `site` selects which chain's store to pin to and which supermarket tag
    to write onto each product (scraper.browser.SITES). Defaults to
    "paknsave" so every existing caller (search_run.py, weekly_combined.py,
    and this module's own default) is unaffected.
    """
    site_config = SITES[site]
    guard = guard or RobotsGuard()
    overrides = load_overrides(overrides_path)
    category_pages = parse_categories_file(categories_path) + list(extra_pages or [])
    starts = [p for p in category_pages if p.page == 1]
    remembered = page_limits.load_page_counts(site)
    page_stats: list[PageStat] = []

    if not category_pages:
        log(f"No categories found in {categories_path} - nothing to scrape.")
        return {"products": [], "stats": _empty_stats(), "scraped_categories": {}, "scraped_at": now_nz_iso()}

    log(f"{len(starts)} categories to be scraped.")

    products: list[dict] = []
    dropped_reasons: list[str] = []
    skipped_pages: list[str] = []
    robots_skipped: list[str] = []
    scraped_categories: dict[str, tuple[str, ...]] = {}  # label -> covers, for categories with a page that loaded
    scraped_at = date.today().isoformat()
    scraped_at_nz = now_nz_iso()
    done_before = 0
    pages_loaded = 0
    wines_set_aside = 0
    urls = [first.url for first in starts]
    saved = checkpoint_module.load(site, urls) if checkpoint else None
    if saved:
        done_before = saved["done"]
        products, dropped_reasons = saved["products"], saved["dropped_reasons"]
        skipped_pages, robots_skipped = saved["skipped_pages"], saved["robots_skipped"]
        scraped_categories, page_stats = saved["scraped_categories"], saved["page_stats"]
        scraped_at, scraped_at_nz = saved["scraped_at"], saved["scraped_at_nz"]
        pages_loaded, wines_set_aside = saved["pages_loaded"], saved["wines_set_aside"]
        log(f"Carrying on from a saved scrape: {done_before} of {len(starts)} categories already read, {len(products)} items.")

    # stealth_playwright() (scraper/browser.py) wraps async_playwright() with
    # playwright-stealth, so every browser/context/page opened below - the
    # only browser session this real scraper opens - automatically gets the
    # same stealth evasions that got step0_verify.py past PAK'nSAVE's
    # Cloudflare challenge.
    async with stealth_playwright() as p:
        launch_kwargs = {"headless": not headed}
        # Optional override for environments with a pre-installed Chromium
        # at a non-default path (e.g. a Docker image with its own browser
        # layer). Not needed on a normal `playwright install`-ed machine.
        chromium_override = os.environ.get("PWSCRAPER_CHROMIUM_PATH")
        if chromium_override:
            launch_kwargs["executable_path"] = chromium_override
        browser = await p.chromium.launch(**launch_kwargs)
        # Pinned to this site's configured store rather than a plain
        # browser.new_page() - see new_pinned_context()'s docstring /
        # README.md for why. Defaults to PAK'nSAVE Petone.
        context = await new_pinned_context(browser, site_config.store_id, site_config.store_cookie_domain)
        page = await context.new_page()

        loaded_any = False
        for number, first in enumerate(starts, 1):
            if number <= done_before:
                continue
            path = foodstuffs_path_of(first.url)
            stat = PageStat(
                label=first.category, path=path, configured=first.max_pages,
                limit=max(first.max_pages, remembered.get(path, 0)),
            )
            page_stats.append(stat)
            base_url = first.url.split("?")[0]
            seen_ids: set[str] = set()
            shelf_start = len(products)
            n = 0
            while n < page_limits.SAFETY_CAP_PAGES:
                n += 1
                url = first.url if n == 1 else f"{base_url}?pg={n}"
                log(f"\n[{number}/{len(starts)}] {first.category} page {n} - {url}")
                if not guard.allows(url):
                    skipped_pages.append(url)
                    robots_skipped.append(url)
                    stat.ended = page_limits.ROBOTS
                    break
                if loaded_any:
                    await polite_pause()
                loaded_any = True
                try:
                    await navigate_and_wait_ready(page, url, log)
                except PageLoadTimeout as e:
                    # A Foodstuffs page past the end of a category loads as a timeout, so
                    # one past the page limit is taken as the end, not as a failure.
                    if n > stat.limit:
                        log(f"  page {n} did not load ({e}) - taken as the end of the category")
                    else:
                        log(f"  PAGE SKIPPED: {e}")
                        skipped_pages.append(url)
                    stat.ended = page_limits.LOAD_FAILED
                    break
                pages_loaded += 1

                scraped_categories.setdefault(first.category, first.covers)
                tiles = await find_product_tiles(page)
                log(f"  {len(tiles)} product tile(s) found")

                page_ids: set[str] = set()
                for tile in tiles:
                    result: ExtractResult = await extract_product(
                        tile, first.category, scraped_at, site_config.supermarket
                    )

                    if result.drop_reason:
                        dropped_reasons.append(result.drop_reason)
                        continue

                    product = result.product
                    override = get_override(overrides, product["supermarket"], product["product_id"])
                    if override is not None:
                        if override.skip:
                            dropped_reasons.append(f"{product['product_id']} {product['name']} - excluded via overrides.txt")
                            continue
                        product = apply_override(product, override)

                    valid, reason = is_valid_product(product)
                    if not valid:
                        dropped_reasons.append(f"{product['product_id']} {product['name']} - {reason}")
                        continue

                    product["store_category"] = path
                    products.append(product)
                    page_ids.add(product["product_id"])

                if not tiles:
                    stat.ended = page_limits.EMPTY_PAGE
                    break
                new_ids = page_ids - seen_ids
                if not new_ids:
                    stat.ended = page_limits.REPEAT_PAGE
                    break
                seen_ids |= page_ids
                full = len(tiles) >= site_config.full_page_tiles
                page_limits.note_page(stat, n, len(tiles), len(new_ids), full)
                if not full:
                    stat.ended = page_limits.SHORT_PAGE
                    break
            else:
                stat.ended = page_limits.SAFETY_CAP
            page_limits.log_cut_short([stat], log)
            if cooking_wine.is_wine_shelf(path):
                kept, set_aside = cooking_wine.select_for_shelf(path, products[shelf_start:], log)
                products[shelf_start:] = kept
                wines_set_aside += set_aside
            if checkpoint:
                try:
                    checkpoint_module.save(site, {
                        "signature": checkpoint_module.signature(urls), "done": number,
                        "products": products, "dropped_reasons": dropped_reasons,
                        "skipped_pages": skipped_pages, "robots_skipped": robots_skipped,
                        "scraped_categories": {k: list(v) for k, v in scraped_categories.items()},
                        "page_stats": [asdict(s) for s in page_stats],
                        "scraped_at": scraped_at, "scraped_at_nz": scraped_at_nz,
                        "pages_loaded": pages_loaded, "wines_set_aside": wines_set_aside,
                    })
                except Exception as e:
                    log(f"  (could not save progress: {type(e).__name__}: {e})")

        await browser.close()

    counts = page_limits.cut_short_counts(page_stats)
    stats = {
        "pages_scraped": pages_loaded,
        "pages_skipped": skipped_pages,
        "robots_skipped": robots_skipped,
        "categories_scraped": len(scraped_categories),
        "products_found": len(products) + len(dropped_reasons) + wines_set_aside,
        "products_saved": len(products),
        "products_with_unit_price": sum(1 for p in products if p["unit_price"] is not None),
        "products_without_unit_price": sum(1 for p in products if p["unit_price"] is None),
        "products_dropped": dropped_reasons,
        "categories_cut_short": counts["cut_short"],
        "extra_pages_read": counts["extra_pages"],
    }
    return {
        "products": products,
        "stats": stats,
        "scraped_categories": scraped_categories,
        "scraped_at": scraped_at_nz,
        "page_stats": page_stats,
    }


def _empty_stats() -> dict:
    return {
        "pages_scraped": 0,
        "pages_skipped": [],
        "robots_skipped": [],
        "categories_scraped": 0,
        "products_found": 0,
        "products_saved": 0,
        "products_with_unit_price": 0,
        "products_without_unit_price": 0,
        "products_dropped": [],
        "categories_cut_short": 0,
        "extra_pages_read": 0,
    }


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
    print("RUN SUMMARY")
    print("=" * 60)
    print(f"Pages scraped successfully : {stats['pages_scraped']}")
    print(f"Pages skipped (load failed or robots.txt): {len(stats['pages_skipped'])}")
    for url in stats["pages_skipped"]:
        print(f"  - {url}")
    print(f"Products found on pages    : {stats['products_found']}")
    print(f"Products saved             : {stats['products_saved']}")
    print(f"  - with a unit price      : {stats['products_with_unit_price']}")
    print(f"  - without a unit price   : {stats['products_without_unit_price']} (expected for e.g. 'each' items)")
    print(f"Categories cut short       : {stats.get('categories_cut_short', 0)}")
    print(f"Extra pages read           : {stats.get('extra_pages_read', 0)}")
    print(f"Products dropped entirely  : {len(stats['products_dropped'])}")
    for reason in stats["products_dropped"]:
        print(f"  - {reason}")
    print("=" * 60)


async def main_async(args: argparse.Namespace) -> int:
    if not args.save:
        log("(Dry Run Mode - nothing will be written to disk. Pass --save to write results.)")

    try:
        result = await scrape_all(args.categories, args.overrides, args.headed, args.site, checkpoint=args.save)
    except Exception as e:
        # Genuinely unexpected error - stop the run rather than continuing
        # in an unknown state.
        log(f"UNEXPECTED ERROR - stopping run: {e!r}")
        return 1

    products = result["products"]
    stats = result["stats"]

    print_table(products)
    print_summary(stats)

    if args.save:
        today = date.today().isoformat()
        snapshot_path = write_snapshot(products, args.snapshots_dir, today)
        write_scrape_meta(snapshot_path, result["scraped_at"])
        checkpoint_module.clear(args.site)
        page_limits.save_page_stats(args.site, result["page_stats"], log)
        _, new_entries = update_price_history(products, args.history_path)
        log(f"Snapshot written to {snapshot_path}")
        log(f"Price history updated: {new_entries} new entr{'y' if new_entries == 1 else 'ies'} added")

        # Best-effort only, and only after the local files above are already
        # written - those are the real record regardless of whether this
        # succeeds. Never runs during a dry run.
        run = publish_run(
            products, args.site, result["scraped_at"], legacy=args.legacy_mailbox, snapshot_path=snapshot_path
        )
        log(f"SUMMARY: {run.summary() or 'nothing posted'}")

    return 0


def main() -> None:
    args = parse_args()
    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
