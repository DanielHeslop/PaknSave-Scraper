"""Plain read-only HTTP transport for Woolworths NZ's public product-search
JSON endpoint.

Deliberately NOT browser automation - no Playwright, no login, no cart. This
is a single `requests.Session()` sending the same headers Woolworths' own
frontend sends for these XHR calls (a real desktop Chrome User-Agent,
Referer: the Woolworths homepage, and X-Requested-With:
OnlineShopping.WebApp - confirmed live 2026-09-17 by calling the endpoint
directly with exactly these headers and getting a normal 200 JSON response,
not a challenge page). This mimics an ordinary browser visitor; it does not
spoof anything the site doesn't already expect from its own web app.

Two endpoints, both confirmed live against the real site:
  - GET /api/v1/products?target=search&search=<term>&inStockProductsOnly=<bool>&size=<n>
    -> {"products": {"items": [...]}}
  - GET /api/v1/products/<sku>
    -> a single product dict, same shape as one "items" entry.

robots.txt (fetched live) disallows /shop/search (the HTML search page) but
NOT /api/v1/products (the JSON endpoint used here) - confirmed before this
module was written.

Bot-detection posture: a 403 or 429 is raised immediately as BotDetected and
logged loudly - never retried, never worked around with a different
User-Agent or header set. Only 5xx responses and network-level errors
(timeouts, connection failures) are retried, with jittered exponential
backoff. tenacity is not an existing dependency of this repo (checked
requirements.txt and the environment - not installed) and pulling it in for
a three-line retry loop would be a new dependency for something this small,
so the backoff is hand-rolled instead, matching this repo's existing style
of hand-rolled retry logic in scraper/browser.py's navigate_and_wait_ready.

Response parsing is defensive throughout: every field is read with .get()
and a fallback, and a single unparsable item (e.g. the "Cartology" ad
placeholder entries the real search endpoint actually returns interleaved
with products - confirmed live, they have sku=None, price={}, size={}) is
skipped with a logged reason rather than crashing the whole search.
"""

from __future__ import annotations

import random
import re
import time
from datetime import date

import requests

from scraper.robots import RobotsDisallowed, RobotsGuard

BASE_URL = "https://www.woolworths.co.nz/api/v1/products"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 15
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0

SUPERMARKET_LABEL = "Woolworths"

# Minimum gap between two requests to Woolworths (any endpoint), plus jitter.
REQUEST_DELAY_SECONDS = 2.0
REQUEST_DELAY_JITTER_SECONDS = 1.0

ROBOTS_GUARD = RobotsGuard()
_last_request_at: float | None = None


def _throttle() -> None:
    global _last_request_at
    if _last_request_at is not None:
        wait = _last_request_at + REQUEST_DELAY_SECONDS + random.uniform(0, REQUEST_DELAY_JITTER_SECONDS) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    _last_request_at = time.monotonic()

# Department/category labels Woolworths' own search endpoint bakes into the
# front of a product's `name` field (confirmed live 2026-09-19, e.g. "fresh
# vegetable red onion (ea)", "fresh fruit bananas yellow loose") - noise from
# Woolworths' own naming, not real product identity. Only a genuine leading
# prefix (this text followed by a space) is stripped; a name that merely
# contains one of these phrases elsewhere (e.g. "woolworths fresh vegetable
# beans green", "the odd bunch fresh vegetable carrots" - both seen live) is
# left untouched, as is an unrelated name that happens to start similarly
# (e.g. "fresh n fruity yoghurt", a real brand name - "fresh n" is not
# "fresh fruit " so it doesn't match).
_NAME_PREFIXES_TO_STRIP = ["fresh vegetable", "fresh vegetables", "fresh fruit", "fresh fruits"]


def _strip_department_prefix(name: str) -> str:
    """Strip a known leading department-label prefix from a product name.

    Case-insensitive match, but the returned text preserves the original
    casing of whatever follows the prefix. A no-op if no prefix matches.
    """
    lowered = name.lower()
    for prefix in _NAME_PREFIXES_TO_STRIP:
        if lowered.startswith(prefix + " "):
            return name[len(prefix):].lstrip()
    return name


class BotDetected(Exception):
    """Raised on a 403/429 response - a hard stop, never retried or routed around."""


def _log(message: str) -> None:
    print(message, flush=True)


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.woolworths.co.nz/",
            "X-Requested-With": "OnlineShopping.WebApp",
            "Accept": "application/json, text/plain, */*",
        }
    )
    return session


def _get_with_retry(session: requests.Session, url: str, params: dict | None = None) -> requests.Response:
    """GET url, retrying only 5xx/network errors with jittered backoff.

    A 403 or 429 raises BotDetected immediately - see module docstring.
    A URL robots.txt disallows raises RobotsDisallowed before any request.
    """
    full_url = requests.Request("GET", url, params=params).prepare().url
    if not ROBOTS_GUARD.allows(full_url):
        raise RobotsDisallowed(full_url)
    attempt = 0
    while True:
        _throttle()
        try:
            response = session.get(url, params=params, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as e:
            attempt += 1
            if attempt > MAX_RETRIES:
                raise
            delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            _log(f"  network error ({e!r}) - retrying in {delay:.1f}s ({attempt}/{MAX_RETRIES})...")
            time.sleep(delay)
            continue

        if response.status_code in (403, 429):
            raise BotDetected(
                f"{url} responded with {response.status_code} - this looks like bot "
                "detection. Stopping this request, not retrying or working around it."
            )

        if response.status_code >= 500:
            attempt += 1
            if attempt > MAX_RETRIES:
                response.raise_for_status()
            delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            _log(
                f"  server error {response.status_code} - retrying in {delay:.1f}s "
                f"({attempt}/{MAX_RETRIES})..."
            )
            time.sleep(delay)
            continue

        return response


def _normalise_cup_measure(measure: str) -> str:
    """Woolworths' own "$X/each" equivalent is a literal "1ea" cupMeasure -
    not a shape scraper.unit_price.parse_raw_unit_price recognises (it knows
    "each"/"ea", carried over from PAK'nSAVE's own displayed text). Map it
    to "each" here rather than teaching the shared, PAK'nSAVE-authored
    parser a Woolworths-specific unit spelling.
    """
    if measure.lower() in ("1ea", "ea", "each"):
        return "each"
    return measure


_PROMO_TAG_LABELS = {
    "IsClubPrice": "Club price",
    "IsSpecial": "Special",
    "IsGreatPrice": "Great price",
}


def _read_promo(item: dict, price: float) -> dict:
    """Promo fields from the public price block and product tag, or {}.

    Sale: salePrice below originalPrice. Member: the same, with isClubPrice.
    Multibuy: only if productTag.multiBuy is filled in (never seen in a live
    sample, so this path is unverified).
    """
    price_block = item.get("price") or {}
    tag = item.get("productTag") or {}
    original = price_block.get("originalPrice")
    ends = price_block.get("promotionEndDate")
    ends = str(ends)[:10] if ends else None

    multibuy = tag.get("multiBuy")
    if multibuy:
        text = multibuy if isinstance(multibuy, str) else (
            multibuy.get("text") or multibuy.get("name") or multibuy.get("description") or ""
        )
        qty = re.match(r"\s*(\d+)\s*for\b", str(text), re.IGNORECASE)
        promo = {"promo_type": "multibuy", "promo_label": str(text) or "Multibuy"}
        if qty:
            promo["promo_min_qty"] = int(qty.group(1))
        if ends:
            promo["promo_ends"] = ends
        return promo

    if original is None or not price < original:
        return {}
    promo_type = "member" if price_block.get("isClubPrice") else "sale"
    label = _PROMO_TAG_LABELS.get(tag.get("tagType") or "", "Club price" if promo_type == "member" else "Special")
    promo = {"promo_type": promo_type, "promo_label": label}
    if ends:
        promo["promo_ends"] = ends
    return promo


def _parse_item(item: dict, fallback_category: str) -> tuple[dict | None, str | None]:
    """Parse one raw search-result/product-lookup item into this repo's
    product dict shape, or return (None, reason) if it can't be read at all.

    Mirrors scraper/extract.py's ExtractResult convention: a missing
    optional field (size, unit price) is a normal partial result, not a
    drop reason. Only a missing sku, name, or price is fatal for this item.
    """
    from scraper.unit_price import parse_raw_unit_price

    sku = item.get("sku")
    name = item.get("name")
    if not sku or not name:
        return None, f"{item.get('name') or '(unnamed)'} - missing sku or name (likely a non-product entry)"
    name = _strip_department_prefix(name)

    price_block = item.get("price") or {}
    price = price_block.get("salePrice")
    if price is None:
        price = price_block.get("originalPrice")
    if price is None:
        return None, f"{sku} {name} - could not read a valid price"

    size_block = item.get("size") or {}
    size = size_block.get("volumeSize")
    if not size and (item.get("unit") or "").lower() == "each":
        size = "ea"

    cup_price = size_block.get("cupPrice")
    cup_measure = size_block.get("cupMeasure")
    cup_text = ""
    unit_price: float | None = None
    unit: str | None = None
    if cup_price is not None and cup_measure:
        cup_text = f"${cup_price}/{_normalise_cup_measure(cup_measure)}"
        parsed = parse_raw_unit_price(cup_text, name)
        if parsed is not None:
            unit_price = parsed.amount
            unit = parsed.unit

    promo = _read_promo(item, price)
    on_promo = promo.get("promo_type") in ("sale", "member")
    regular_pack_price = price_block.get("originalPrice") if on_promo else price
    if regular_pack_price is None:
        regular_pack_price = price
    list_price = size_block.get("cupListPrice")
    if on_promo:
        # cupPrice is the promo unit price; cupListPrice the regular one (0.0 when absent).
        promo["promo_unit_raw"] = cup_text
        unit_raw = f"${list_price}/{_normalise_cup_measure(cup_measure)}" if list_price and cup_measure else ""
    else:
        unit_raw = cup_text

    departments = item.get("departments") or []
    category = departments[0].get("name") if departments and departments[0].get("name") else fallback_category

    return {
        "product_id": str(sku),
        "name": name,
        "category": category,
        "size": size,
        "price": price,
        "unit_price": unit_price,
        "unit": unit,
        "scraped_at": date.today().isoformat(),
        "supermarket": SUPERMARKET_LABEL,
        # Raw inputs for scraper/item_fields.py. "price" above stays the price
        # charged now (sale price when there is one).
        "unit_raw": unit_raw,
        "pack_price": price,
        "regular_pack_price": regular_pack_price,
        "package_type": size_block.get("packageType"),
        **promo,
    }, None


def search_products(term: str, size: int = 30) -> tuple[list[dict], list[str]]:
    """Search Woolworths by free-text term. Returns (products, skip_reasons).

    inStockProductsOnly=true mirrors what a real shopper sees by default -
    this is a price-tracking tool, not an out-of-stock catalogue.
    """
    session = _new_session()
    response = _get_with_retry(
        session,
        BASE_URL,
        params={"target": "search", "search": term, "inStockProductsOnly": "true", "size": size},
    )
    data = response.json()
    items = (data.get("products") or {}).get("items") or []

    products: list[dict] = []
    skip_reasons: list[str] = []
    for item in items:
        product, reason = _parse_item(item, fallback_category=term)
        if reason is not None:
            skip_reasons.append(reason)
            continue
        products.append(product)

    return products, skip_reasons


BROWSE_PAGE_SIZE = 120


def browse_url_filters(path: str) -> list[tuple[str, str]]:
    """'pantry/baking' -> Department + Aisle filters; a third part adds a Shelf."""
    parts = [p for p in path.strip("/").split("/") if p]
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"browse path needs department[/aisle[/shelf]], got {path!r}")
    return [
        ("dasFilter", f"{level};;{value};false")
        for level, value in zip(("Department", "Aisle", "Shelf"), parts)
    ]


def browse_products(path: str, max_pages: int = 5, label: str | None = None) -> tuple[list[dict], list[str], int]:
    """Read one browse path (department/aisle/shelf) from the public browse API.

    Returns (products, skip_reasons, total_items_reported). Pages of 120 are read
    until the total is covered or max_pages is hit; every request goes through
    the throttle and the robots guard.
    """
    session = _new_session()
    fallback = label or path.strip("/").split("/")[-1]
    products: list[dict] = []
    skip_reasons: list[str] = []
    total = 0
    for page in range(1, max_pages + 1):
        params = [
            ("target", "browse"),
            *browse_url_filters(path),
            ("inStockProductsOnly", "true"),
            ("size", str(BROWSE_PAGE_SIZE)),
            ("page", str(page)),
        ]
        data = _get_with_retry(session, BASE_URL, params=params).json()
        block = data.get("products") or {}
        items = block.get("items") or []
        total = block.get("totalItems") or total
        for item in items:
            product, reason = _parse_item(item, fallback_category=fallback)
            if reason is not None:
                skip_reasons.append(reason)
            else:
                products.append(product)
        if not items or page * BROWSE_PAGE_SIZE >= total:
            break
    return products, skip_reasons, total


def get_product_by_stock_code(stock_code: str) -> dict | None:
    """Look up a single Woolworths product by its stock code (sku).

    Mirrors what a targeted re-pricing refresh would need - the Woolworths
    equivalent of re-checking one already-known PAK'nSAVE product_id.
    Returns None (with a logged reason) if the product can't be read.
    """
    session = _new_session()
    response = _get_with_retry(session, f"{BASE_URL}/{stock_code}")
    if response.status_code == 404:
        _log(f"  {stock_code} - not found (404)")
        return None

    item = response.json()
    product, reason = _parse_item(item, fallback_category="uncategorised")
    if reason is not None:
        _log(f"  {stock_code} - {reason}")
        return None
    return product


def get_product_description_text(stock_code: str) -> str | None:
    """Description plus claims text for one product, from the public product page API.

    One throttled request through the same robots-checked, retrying path as
    every other Woolworths call. Returns None when the product can't be read.
    """
    session = _new_session()
    response = _get_with_retry(session, f"{BASE_URL}/{stock_code}")
    if response.status_code == 404:
        return None
    data = response.json()
    parts = [data.get("description") or ""]
    claims = data.get("claims")
    if isinstance(claims, list):
        parts.extend(str(c) for c in claims)
    elif claims:
        parts.append(str(claims))
    return "\n".join(parts)
