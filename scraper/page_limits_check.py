"""Dry-run check: which categories' last page was full at the page limit? No network, no writes.

    python3 -m scraper.page_limits_check [--store paknsave|newworld|woolworths]

Works from the categories files, the remembered page counts and the latest snapshot (and,
for Woolworths, the latest log, which records each shelf's total). A snapshot does not keep
page boundaries, so the rule is the plain one: a category whose snapshot holds at least
limit x full-page products had a full last page at its limit, and so may be cut short.
A real run settles it by reading on.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
from collections import Counter

from scraper import category_tree, page_limits
from scraper.browser import SITES, parse_categories_file
from scraper.category_mapping import MAPPED_FOODSTUFFS_MAX_PAGES, label_for_record, load_mappings
from scraper.store_category import foodstuffs_path_of

WOOLWORTHS_PAGE_ITEMS = 120


def latest_file(pattern: str) -> str | None:
    files = sorted(f for f in glob.glob(pattern) if not f.endswith(".meta.json"))
    return files[-1] if files else None


def foodstuffs_categories(store: str, categories_file: str | None = None) -> list[tuple[str, str, int]]:
    """(label, path, configured pages) for every category the scraper reads: the file, then mapped ones."""
    site = SITES[store]
    pages = parse_categories_file(categories_file or os.path.join(category_tree.REPO_ROOT, site.default_categories_file))
    found = [(p.category, foodstuffs_path_of(p.url), p.max_pages) for p in pages if p.page == 1]
    have = {path for _, path, _ in found}
    for record in load_mappings(store).values():
        if record["path"] not in have:
            have.add(record["path"])
            found.append((label_for_record(record), record["path"], MAPPED_FOODSTUFFS_MAX_PAGES))
    return found


def check_foodstuffs(store: str, categories_file: str | None = None) -> tuple[str | None, list[dict]]:
    site = SITES[store]
    snapshot = latest_file(os.path.join(category_tree.STATE_DIR, site.default_snapshots_subdir, "*.json"))
    if not snapshot:
        return None, []
    products = json.load(open(snapshot))
    products = products if isinstance(products, list) else products.get("products", [])
    by_label = Counter(p.get("category") for p in products)
    remembered = page_limits.load_page_counts(store)
    rows = []
    for label, path, configured in foodstuffs_categories(store, categories_file):
        limit = max(configured, remembered.get(path, 0))
        items = by_label.get(label, 0)
        last = items - (limit - 1) * site.full_page_tiles
        rows.append({
            "label": label, "path": path, "limit": limit, "items": items,
            "last_page_items": max(0, min(last, site.full_page_tiles)) if items else 0,
            "full_at_limit": items >= limit * site.full_page_tiles,
        })
    return snapshot, rows


def check_woolworths() -> tuple[str | None, list[dict]]:
    from scraper import woolworths

    log_file = latest_file(os.path.join(category_tree.REPO_ROOT, "logs", "woolworths-*.log"))
    if not log_file:
        return None, []
    totals: dict[str, int] = {}
    path = None
    for line in open(log_file, errors="replace"):
        m = re.match(r"\[\d+/\d+\] browsing: (\S+)", line)
        if m:
            path = m.group(1)
            continue
        m = re.match(r"\s+\d+ of (\d+) product\(s\) read", line)
        if m and path:
            totals[path] = int(m.group(1))
            path = None
    remembered = page_limits.load_page_counts("woolworths")
    rows = []
    for page in woolworths.parse_categories_file(woolworths.DEFAULT_CATEGORIES_PATH):
        limit = max(page.max_pages, remembered.get(page.path, 0))
        items = totals.get(page.path)
        if items is None:
            rows.append({"label": page.label, "path": page.path, "limit": limit, "items": None,
                         "last_page_items": None, "full_at_limit": False})
            continue
        pages = max(1, math.ceil(items / WOOLWORTHS_PAGE_ITEMS))
        rows.append({
            "label": page.label, "path": page.path, "limit": limit, "items": items,
            "last_page_items": items - (pages - 1) * WOOLWORTHS_PAGE_ITEMS,
            "full_at_limit": items >= limit * WOOLWORTHS_PAGE_ITEMS,
        })
    return log_file, rows


def report(store: str, categories_file: str | None = None) -> list[dict]:
    source, rows = check_woolworths() if store == "woolworths" else check_foodstuffs(store, categories_file)
    print(f"\n{store}: {os.path.relpath(source, category_tree.REPO_ROOT) if source else 'no snapshot or log found'}")
    for r in rows:
        mark = "FULL AT LIMIT" if r["full_at_limit"] else "ok"
        items = "not in log" if r["items"] is None else f"{r['items']} items, last page {r['last_page_items']}"
        print(f"  {r['label']:<28} limit {r['limit']}  {items:<34} {mark}")
    flagged = [r for r in rows if r["full_at_limit"]]
    print(f"  -> {len(flagged)} of {len(rows)} categories had a full last page at the limit"
          + (": " + ", ".join(r["label"] for r in flagged) if flagged else ""))
    return flagged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--store", choices=["paknsave", "newworld", "woolworths"])
    parser.add_argument("--categories", help="use this categories file instead of the store's own (one store)")
    args = parser.parse_args(argv)
    if args.categories and not args.store:
        parser.error("--categories needs --store")
    for store in [args.store] if args.store else ["paknsave", "newworld", "woolworths"]:
        report(store, args.categories)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
