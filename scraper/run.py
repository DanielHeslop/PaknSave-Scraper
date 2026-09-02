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
from datetime import date, datetime, timezone

from scraper.browser import (
    PageLoadTimeout,
    navigate_and_wait_ready,
    parse_categories_file,
    stealth_playwright,
)
from scraper.extract import ExtractResult, extract_product, find_product_tiles, is_valid_product
from scraper.overrides import load_overrides
from scraper.publish import publish_to_mailbox
from scraper.storage import update_price_history, write_snapshot
from scraper.unit_price import derive_from_size_and_price

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CATEGORIES_PATH = os.path.join(REPO_ROOT, "categories.txt")
DEFAULT_OVERRIDES_PATH = os.path.join(REPO_ROOT, "overrides.txt")
DEFAULT_SNAPSHOTS_DIR = os.path.join(REPO_ROOT, "data", "snapshots")
DEFAULT_HISTORY_PATH = os.path.join(REPO_ROOT, "data", "price_history.json")


def log(message: str) -> None:
    print(message, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PAK'nSAVE price-tracking scraper (read-only).")
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
    parser.add_argument("--categories", default=DEFAULT_CATEGORIES_PATH)
    parser.add_argument("--overrides", default=DEFAULT_OVERRIDES_PATH)
    parser.add_argument("--snapshots-dir", default=DEFAULT_SNAPSHOTS_DIR)
    parser.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)
    return parser.parse_args(argv)


def apply_override(product: dict, override) -> dict:
    """Apply a size (and recalculated unit price) or skip override in place."""
    if override.size:
        product["size"] = override.size
        derived = derive_from_size_and_price(override.size, product["price"])
        if derived is not None:
            product["unit_price"] = derived.amount
            product["unit"] = derived.unit
    return product


async def scrape_all(categories_path: str, overrides_path: str, headed: bool) -> dict:
    """Run the full scrape and return a results dict with products and stats."""
    overrides = load_overrides(overrides_path)
    category_pages = parse_categories_file(categories_path)

    if not category_pages:
        log(f"No categories found in {categories_path} - nothing to scrape.")
        return {"products": [], "stats": _empty_stats()}

    log(f"{len(category_pages)} page(s) to be scraped.")

    products: list[dict] = []
    dropped_reasons: list[str] = []
    skipped_pages: list[str] = []
    scraped_at = date.today().isoformat()

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
        page = await browser.new_page()

        for i, category_page in enumerate(category_pages, start=1):
            log(f"\n[{i}/{len(category_pages)}] {category_page.category} - {category_page.url}")
            try:
                await navigate_and_wait_ready(page, category_page.url, log)
            except PageLoadTimeout as e:
                log(f"  PAGE SKIPPED: {e}")
                skipped_pages.append(category_page.url)
                continue

            tiles = await find_product_tiles(page)
            log(f"  {len(tiles)} product tile(s) found")

            for tile in tiles:
                result: ExtractResult = await extract_product(tile, category_page.category, scraped_at)

                if result.drop_reason:
                    dropped_reasons.append(result.drop_reason)
                    continue

                product = result.product
                override = overrides.get(product["product_id"])
                if override is not None:
                    if override.skip:
                        dropped_reasons.append(f"{product['product_id']} {product['name']} - excluded via overrides.txt")
                        continue
                    product = apply_override(product, override)

                valid, reason = is_valid_product(product)
                if not valid:
                    dropped_reasons.append(f"{product['product_id']} {product['name']} - {reason}")
                    continue

                products.append(product)

        await browser.close()

    stats = {
        "pages_scraped": len(category_pages) - len(skipped_pages),
        "pages_skipped": skipped_pages,
        "products_found": len(products) + len(dropped_reasons),
        "products_saved": len(products),
        "products_with_unit_price": sum(1 for p in products if p["unit_price"] is not None),
        "products_without_unit_price": sum(1 for p in products if p["unit_price"] is None),
        "products_dropped": dropped_reasons,
    }
    return {"products": products, "stats": stats}


def _empty_stats() -> dict:
    return {
        "pages_scraped": 0,
        "pages_skipped": [],
        "products_found": 0,
        "products_saved": 0,
        "products_with_unit_price": 0,
        "products_without_unit_price": 0,
        "products_dropped": [],
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
    print(f"Pages skipped (load failed): {len(stats['pages_skipped'])}")
    for url in stats["pages_skipped"]:
        print(f"  - {url}")
    print(f"Products found on pages    : {stats['products_found']}")
    print(f"Products saved             : {stats['products_saved']}")
    print(f"  - with a unit price      : {stats['products_with_unit_price']}")
    print(f"  - without a unit price   : {stats['products_without_unit_price']} (expected for e.g. 'each' items)")
    print(f"Products dropped entirely  : {len(stats['products_dropped'])}")
    for reason in stats["products_dropped"]:
        print(f"  - {reason}")
    print("=" * 60)


async def main_async(args: argparse.Namespace) -> int:
    if not args.save:
        log("(Dry Run Mode - nothing will be written to disk. Pass --save to write results.)")

    try:
        result = await scrape_all(args.categories, args.overrides, args.headed)
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
        _, new_entries = update_price_history(products, args.history_path)
        log(f"Snapshot written to {snapshot_path}")
        log(f"Price history updated: {new_entries} new entr{'y' if new_entries == 1 else 'ies'} added")

        # Best-effort only, and only after the local files above are already
        # written - those are the real record regardless of whether this
        # succeeds. Never runs during a dry run.
        publish_to_mailbox(products)

    return 0


def main() -> None:
    args = parse_args()
    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
