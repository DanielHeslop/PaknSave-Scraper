"""Entry point for the Woolworths NZ scrape: sweep woolworths_terms.txt's
search terms against Woolworths' public product-search JSON endpoint.

Independent of and additional to scraper/run.py's PAK'nSAVE category scrape
- a separate supermarket chain, a separate config file, a separate
transport (plain requests, not Playwright) - but reusing the same
persistence functions (scraper/storage.py) and mailbox publish step
(scraper/publish.py) so both chains' output lands in the same
data/snapshots/, data/price_history.json, and mailbox payload, tagged with
their own "supermarket" field.

Default mode is a DRY RUN, same convention as scraper/run.py and
scraper/search_run.py: prints a table, writes nothing to disk. Pass --save
to write results for real.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from scraper.extract import is_valid_product
from scraper.publish import publish_to_mailbox
from scraper.storage import update_price_history, write_snapshot
from scraper.woolworths_transport import BotDetected, search_products

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TERMS_PATH = os.path.join(REPO_ROOT, "scraper", "woolworths_terms.txt")
# Separate snapshot directory from PAK'nSAVE's data/snapshots/ - same reason
# scraper/search_run.py uses its own data/search_snapshots/ rather than
# sharing PAK'nSAVE's daily dir: write_snapshot() replaces a whole
# <date>.json file, so two unrelated scrapes writing to the same directory
# on the same date would silently clobber each other. price_history.json IS
# shared (see module docstring) since it's keyed per product_id and only
# ever appended to, not replaced.
DEFAULT_SNAPSHOTS_DIR = os.path.join(REPO_ROOT, "data", "woolworths_snapshots")
DEFAULT_HISTORY_PATH = os.path.join(REPO_ROOT, "data", "price_history.json")


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
    parser.add_argument("--terms", default=DEFAULT_TERMS_PATH)
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


def scrape_all(terms_path: str, size: int) -> dict:
    """Sweep every term in terms_path and return a results dict, mirroring
    scraper/run.py's scrape_all() shape (products + stats).
    """
    terms = parse_terms_file(terms_path)
    if not terms:
        log(f"No search terms found in {terms_path} - nothing to scrape.")
        return {"products": [], "stats": _empty_stats()}

    log(f"{len(terms)} search term(s) to sweep.")

    products: list[dict] = []
    dropped_reasons: list[str] = []
    terms_blocked: list[str] = []

    for i, term in enumerate(terms, start=1):
        log(f"\n[{i}/{len(terms)}] searching: {term!r}")
        try:
            term_products, skip_reasons = search_products(term, size=size)
        except BotDetected as e:
            # Hard stop for this term only - not escalated, not retried with
            # different headers, not routed around. The rest of the run
            # still gets a chance (a block can be term-specific rate
            # limiting), but every blocked term is reported plainly.
            log(f"  BLOCKED: {e}")
            terms_blocked.append(f"{term} - {e}")
            continue

        log(f"  {len(term_products)} product(s) found, {len(skip_reasons)} skipped")
        for reason in skip_reasons:
            dropped_reasons.append(f"[{term}] {reason}")

        for product in term_products:
            valid, reason = is_valid_product(product)
            if not valid:
                dropped_reasons.append(f"{product['product_id']} {product['name']} - {reason}")
                continue
            products.append(product)

    stats = {
        "terms_searched": len(terms) - len(terms_blocked),
        "terms_blocked": terms_blocked,
        "products_found": len(products) + len(dropped_reasons),
        "products_saved": len(products),
        "products_with_unit_price": sum(1 for p in products if p["unit_price"] is not None),
        "products_without_unit_price": sum(1 for p in products if p["unit_price"] is None),
        "products_dropped": dropped_reasons,
    }
    return {"products": products, "stats": stats}


def _empty_stats() -> dict:
    return {
        "terms_searched": 0,
        "terms_blocked": [],
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
    print("WOOLWORTHS RUN SUMMARY")
    print("=" * 60)
    print(f"Terms searched successfully : {stats['terms_searched']}")
    print(f"Terms blocked (403/429)     : {len(stats['terms_blocked'])}")
    for reason in stats["terms_blocked"]:
        print(f"  - {reason}")
    print(f"Products found              : {stats['products_found']}")
    print(f"Products saved              : {stats['products_saved']}")
    print(f"  - with a unit price       : {stats['products_with_unit_price']}")
    print(f"  - without a unit price    : {stats['products_without_unit_price']}")
    print(f"Products dropped entirely   : {len(stats['products_dropped'])}")
    for reason in stats["products_dropped"]:
        print(f"  - {reason}")
    print("=" * 60)


def main() -> int:
    args = parse_args()
    if not args.save:
        log("(Dry Run Mode - nothing will be written to disk. Pass --save to write results.)")

    try:
        result = scrape_all(args.terms, args.size)
    except Exception as e:
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

        publish_to_mailbox(products)

    return 0


if __name__ == "__main__":
    sys.exit(main())
