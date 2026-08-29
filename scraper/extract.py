"""Read price, size, and unit price off a single product tile.

Assumptions carried over from the reference project (Jason-nzd/pakn-scraper)
about PAK'nSAVE's page structure - NOT independently verified against the
live site in this environment (network policy blocked reaching
paknsave.co.nz). See README.md "Known limitation" before trusting output:

  - Product tiles are <div> elements whose data-testid attribute contains
    "-EA-000" (sold each) or "-KGM-000" (sold by weight).
  - Price is split across two elements: data-testid="price-dollars" and
    data-testid="price-cents".
  - Size is in a data-testid="product-subtitle" element.
  - Unit price is PAK'nSAVE's own displayed text somewhere in the tile,
    shaped like "$0.49/100g".

Deliberately NOT replicated from the reference project: guessing a size out
of the product's title text. If product-subtitle is blank, size stays None
rather than being inferred - a labelled field or an honest blank is more
trustworthy than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.async_api import ElementHandle

from scraper.unit_price import parse_raw_unit_price

PRODUCT_TILE_TESTID_PATTERN = re.compile(r"-(EA|KGM)-000")


@dataclass
class ExtractResult:
    """Outcome of trying to read one product tile.

    Exactly one of `product` or `drop_reason` is set. A product with
    price=None, size=None etc. is still a successful extraction (a real,
    honest partial result) - it only becomes a drop when the ID, name, or
    price itself can't be read, or basic sanity checks fail.
    """

    product: dict | None = None
    drop_reason: str | None = None


async def find_product_tiles(page) -> list[ElementHandle]:
    """Find all product tile elements on the current page."""
    all_divs = await page.query_selector_all("div")
    tiles = []
    for div in all_divs:
        test_id = await div.get_attribute("data-testid")
        if test_id and PRODUCT_TILE_TESTID_PATTERN.search(test_id):
            tiles.append(div)
    return tiles


async def _get_product_id(tile: ElementHandle) -> str | None:
    """Derive the product ID from the tile's product image filename."""
    try:
        img_elements = await tile.query_selector_all("a > div > img")
        if not img_elements:
            return None
        img_url = await img_elements[-1].get_attribute("src")
        if not img_url:
            return None
        filename = img_url.split("/")[-1].split("?")[0]
        stem = filename.split(".")[0]
        return f"P{stem}" if stem else None
    except Exception:
        return None


async def extract_product(tile: ElementHandle, category: str, scraped_at: str) -> ExtractResult:
    """Read one product tile into a dict of raw scraped values.

    Returns a dict with keys: product_id, name, size, price, unit_price,
    unit, category, scraped_at - or a drop_reason if the product could not
    be read at all.
    """
    name = ""
    size: str | None = None
    dollar_text = ""
    cent_text = ""

    p_elements = await tile.query_selector_all("p")
    for p in p_elements:
        try:
            test_id = await p.get_attribute("data-testid")
        except Exception:
            continue

        if test_id == "product-title":
            name = (await p.inner_text()).strip()
        elif test_id == "product-subtitle":
            raw_size = (await p.inner_text()).strip()
            size = raw_size if raw_size else None
        elif test_id == "price-dollars":
            dollar_text = (await p.inner_text()).strip()
        elif test_id == "price-cents":
            cent_text = (await p.inner_text()).strip()

    product_id = await _get_product_id(tile)
    if not product_id:
        return ExtractResult(drop_reason=f"{name or '(unnamed)'} - could not read product ID")

    if not name:
        return ExtractResult(drop_reason=f"{product_id} - could not read product name")

    # Price: both pieces must be present and numeric, or the price is
    # genuinely unreadable for this product - which is grounds to drop it
    # (a product with no price at all fails the later sanity check anyway).
    price: float | None = None
    if dollar_text and cent_text:
        try:
            price = float(f"{dollar_text}.{cent_text}")
        except ValueError:
            price = None

    if price is None:
        return ExtractResult(drop_reason=f"{product_id} {name} - could not read a valid price")

    # Unit price: search every <p> in the tile for PAK'nSAVE's own
    # "$X/Yunit" text. A missing unit price is a normal outcome (e.g.
    # "Each" items with nothing to compare per-unit) - not a drop reason.
    unit_price: float | None = None
    unit: str | None = None
    for p in reversed(p_elements):
        try:
            text = await p.inner_text()
        except Exception:
            continue
        parsed = parse_raw_unit_price(text)
        if parsed is not None:
            unit_price = parsed.amount
            unit = parsed.unit
            break

    return ExtractResult(
        product={
            "product_id": product_id,
            "name": name,
            "category": category,
            "size": size,
            "price": price,
            "unit_price": unit_price,
            "unit": unit,
            "scraped_at": scraped_at,
        }
    )


def is_valid_product(product: dict) -> tuple[bool, str | None]:
    """Basic sanity checks, mirroring the reference project's IsValidProduct().

    Returns (True, None) if valid, or (False, reason) if not.
    """
    name = product.get("name") or ""
    if not name:
        return False, "empty product name"
    if len(name) > 200:
        return False, "product name implausibly long"

    price = product.get("price")
    if price is None or not (0 < price <= 999):
        return False, f"price out of expected range ($0-$999): {price}"

    return True, None
