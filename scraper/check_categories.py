"""Check each store's categories file against its cached category tree.

    python -m scraper.check_categories              # report only
    python -m scraper.check_categories --fix        # also rewrite paths that have one clear replacement

Pak'nSave / New World pages not in the tree are fixed only when a path with the same parent and
nearly the same name is the one clear match; the rest are listed and left alone. Nothing here
touches the network.
"""

from __future__ import annotations

import argparse
import sys

from scraper import category_tree
from scraper.browser import SITES
from scraper.category_mapping import apply_replacements, check_categories_file

WOOLWORTHS_FILE = "woolworths_categories.txt"


def files_by_store() -> dict[str, str]:
    files = {key: site.default_categories_file for key, site in SITES.items()}
    files["woolworths"] = WOOLWORTHS_FILE
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fix", action="store_true", help="rewrite paths that have one clear replacement")
    parser.add_argument("--store", choices=sorted(files_by_store()), help="one store only")
    args = parser.parse_args(argv)

    for store, file in files_by_store().items():
        if args.store and args.store != store:
            continue
        cached = category_tree.load_cache(store)
        if not cached:
            print(f"{store}: no cached category tree - skipped")
            continue
        path = f"{category_tree.REPO_ROOT}/{file}"
        check = check_categories_file(path, cached["categories"], store)
        print(f"{store} ({file}): {len(check.in_tree)} in the tree, {len(check.fixed)} fixable, {len(check.unresolved)} not in the tree")
        for old, new in check.fixed:
            print(f"  fix: {old} -> {new}")
        for old in check.unresolved:
            print(f"  not in the tree, left alone: {old}")
        if args.fix and check.fixed and store != "woolworths":
            changed = apply_replacements(path, dict(check.fixed))
            print(f"  rewrote {changed} line(s) in {file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
