"""Send one store's category tree to the app by hand, and show how the watch categories map.

    python -m scraper.post_categories --site paknsave            # post if changed
    python -m scraper.post_categories --site woolworths --dry-run # read and report only
    python -m scraper.post_categories --site newworld --map       # also map the watch categories

The tree is read from the store's own category sitemap (Pak'nSave, New World) or
navigation (Woolworths), robots.txt honoured, and posted to the app only when it
changed since the last successful post. Products are never read or sent here.
--dry-run posts nothing and writes nothing (no cache, no mapping file).
"""

from __future__ import annotations

import argparse
import sys

from scraper.category_mapping import log_mapping, map_watch_categories
from scraper.category_tree import STORES, log, sync_tree
from scraper.ingredients import DEFAULT_INGREDIENT_LIST_URL, fetch_store_lists


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post a store's category tree to the app.")
    parser.add_argument("--site", choices=STORES, required=True)
    parser.add_argument("--dry-run", action="store_true", help="Read and report only: post and write nothing.")
    parser.add_argument("--map", action="store_true", help="Also map the app's watch categories for this store.")
    parser.add_argument("--ingredient-list-url", default=DEFAULT_INGREDIENT_LIST_URL)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    synced = sync_tree(args.site, save=not args.dry_run)
    if not synced.categories:
        return 1
    if args.map:
        lists = fetch_store_lists(args.site, args.ingredient_list_url)
        if lists is None:
            log("Could not read the app's list - nothing mapped.")
            return 1
        log_mapping(map_watch_categories(args.site, lists.watch_category_specs(), synced.categories, save=not args.dry_run))
    if not args.dry_run and synced.changed and not synced.posted:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
