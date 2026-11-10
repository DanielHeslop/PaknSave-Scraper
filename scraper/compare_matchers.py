"""Compare the app's matcher with the local one on a saved snapshot. Read-only:
nothing is published, reported or written.

    python -m scraper.compare_matchers --site newworld --snapshot data/newworld_snapshots/2026-11-06.json

Needs MAILBOX_URL / MAILBOX_TOKEN in the environment (source the env file first).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from scraper.app_matcher import AppMatchError, match_terms
from scraper.ingredients import DEFAULT_INGREDIENT_LIST_URL, STORES, fetch_store_lists
from scraper.matching import match_products
from scraper.search_step import dedupe_products, plan_terms


def _names(ids, by_id) -> str:
    return "; ".join(f"{i} {by_id[i]['name']}" for i in ids if i in by_id) or "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", choices=STORES, required=True)
    parser.add_argument("--snapshot", required=True, help="a saved snapshot JSON file")
    parser.add_argument("--ingredient-list-url", default=DEFAULT_INGREDIENT_LIST_URL)
    args = parser.parse_args(argv)

    products = dedupe_products(json.load(open(args.snapshot)))
    lists = fetch_store_lists(args.site, args.ingredient_list_url)
    if lists is None:
        return 1
    terms, skipped = plan_terms(lists)
    by_id = {str(p["product_id"]): p for p in products}
    print(f"{args.site}: {len(terms)} term(s) ({len(skipped)} skipped), {len(products)} product(s) from {args.snapshot}")

    started = time.monotonic()
    try:
        app = match_terms(args.site, terms, products)
    except AppMatchError as e:
        print(f"App matcher call failed: {e}")
        return 1
    seconds = time.monotonic() - started

    local = {t: [str(p["product_id"]) for p in match_products(t, products)] for t in terms}
    app_found = {t for t in terms if app.results[t].found}
    local_found = {t for t in terms if local[t]}
    print(f"endpoint version {app.version}, call time {seconds:.2f}s")
    print(f"app   : {len(app_found)} found, {len(terms) - len(app_found)} not found")
    print(f"local : {len(local_found)} found, {len(terms) - len(local_found)} not found")
    via = {t: app.results[t].via for t in terms if app.results[t].via}
    print(f"found via a fallback key (app): {len(via)}")

    print("\nTerms where found / not found differs:")
    for t in terms:
        r = app.results[t]
        if r.found != bool(local[t]):
            via_note = f" via {r.via}" if r.via else ""
            print(f"- {t}: app {'found' + via_note if r.found else 'not found'}: {_names(r.product_ids, by_id)}")
            print(f"  {' ' * len(t)}  local {'found' if local[t] else 'not found'}: {_names(local[t], by_id)}")
    print("\nTerms found by both but with different products:")
    for t in terms:
        r = app.results[t]
        if r.found and local[t] and set(r.product_ids) != set(local[t]):
            print(f"- {t}: {len(r.product_ids)} by the app, {len(local[t])} locally")
            print(f"  app only  : {_names([i for i in r.product_ids if i not in local[t]], by_id)}")
            print(f"  local only: {_names([i for i in local[t] if i not in r.product_ids], by_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
