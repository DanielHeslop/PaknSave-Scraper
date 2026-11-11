"""Fill in sheets-per-item for Woolworths per-100-sheets products.

Search and browse results never say how many sheets a roll has; the product
page description sometimes does ("180 sheets per roll"). For each product
priced per 100 sheets with no sheet count in its name or size, look the
description up once (throttled like every other Woolworths request), keep
the answer in a small cache so later runs don't ask again, and only record a
number when the text names exactly one.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

from scraper.pack_size import parse_pack, sheets_from_text

DEFAULT_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "woolworths_sheets_cache.json"
)
MAX_LOOKUPS_PER_RUN = 120
CACHE_DAYS = 60


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _fresh(entry: dict, today: date) -> bool:
    try:
        return date.fromisoformat(entry["checked"]) >= today - timedelta(days=CACHE_DAYS)
    except (KeyError, ValueError, TypeError):
        return False


def needs_sheet_lookup(product: dict) -> bool:
    if product.get("unit") != "100sheets":
        return False
    pack = parse_pack(product.get("name"), product.get("size"), product.get("package_type"), None)
    return pack.sheets_per_item is None


def enrich_sheet_counts(
    products: list[dict],
    fetch_text,
    cache_path: str = DEFAULT_CACHE_PATH,
    save_cache: bool = False,
    limit: int = MAX_LOOKUPS_PER_RUN,
    log=print,
    today: date | None = None,
) -> int:
    """Set product["sheets_per_item"] where a description gives one. Returns lookups made.

    fetch_text(sku) returns description text or None. A failed lookup leaves
    the product untouched (and uncached) so it is retried next run.
    """
    today = today or date.today()
    cache = _load(cache_path)
    lookups = 0
    changed = False
    for product in products:
        if not needs_sheet_lookup(product):
            continue
        sku = product["product_id"]
        entry = cache.get(sku)
        if entry and _fresh(entry, today):
            if entry.get("sheets"):
                product["sheets_per_item"] = entry["sheets"]
            continue
        if lookups >= limit:
            continue
        lookups += 1
        try:
            text = fetch_text(sku)
        except Exception as e:
            if type(e).__name__ == "BotDetected":
                raise
            log(f"  sheet lookup for {sku} failed ({type(e).__name__}) - skipped")
            continue
        sheets = sheets_from_text(text) if text else None
        cache[sku] = {"sheets": sheets, "checked": today.isoformat()}
        changed = True
        if sheets:
            product["sheets_per_item"] = sheets
    if changed and save_cache:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
    return lookups
