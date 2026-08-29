"""Tests for extract.py, run against a saved fixture page rather than the
live site (see tests/fixtures/sample_products.html for why).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

from scraper.extract import extract_product, find_product_tiles, is_valid_product

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_products.html"

# Optional override for this sandbox's mismatched bundled Chromium revision.
# Not needed on a normal `playwright install`-ed machine.
_CHROMIUM_OVERRIDE = os.environ.get("PW_TEST_CHROMIUM_PATH")


async def _load_fixture_tiles():
    async with async_playwright() as p:
        launch_kwargs = {"headless": True}
        if _CHROMIUM_OVERRIDE:
            launch_kwargs["executable_path"] = _CHROMIUM_OVERRIDE
        browser = await p.chromium.launch(**launch_kwargs)
        page = await browser.new_page()
        # The fixture's <img> tags point at real (unreachable, sandboxed)
        # URLs purely so their filename can be parsed for a product ID -
        # abort the actual image fetches, we never need their bytes.
        await page.route("**/fsimg.co.nz/**", lambda route: route.abort())
        await page.goto(FIXTURE_PATH.as_uri())
        tiles = await find_product_tiles(page)
        results = []
        for tile in tiles:
            result = await extract_product(tile, category="test-category", scraped_at="2026-08-29")
            results.append(result)
        await browser.close()
        return results


def _run():
    return asyncio.run(_load_fixture_tiles())


def test_finds_only_product_tiles_not_other_divs():
    results = _run()
    # 4 product-tile divs in the fixture; the "site-header" div must be excluded.
    assert len(results) == 4


def test_normal_product_with_full_fields():
    results = _run()
    milk = next(r for r in results if r.product and r.product["product_id"] == "P5012345")
    assert milk.product["name"] == "Anchor Blue Milk Powder"
    assert milk.product["size"] == "1kg"
    assert milk.product["price"] == 21.99
    assert milk.product["unit_price"] == 21.99
    assert milk.product["unit"] == "kg"


def test_each_item_has_no_unit_price_and_is_not_an_error():
    results = _run()
    slice_ = next(r for r in results if r.product and r.product["product_id"] == "P3457825")
    assert slice_.drop_reason is None
    assert slice_.product["price"] == 5.89
    assert slice_.product["unit_price"] is None
    assert slice_.product["unit"] is None
    valid, reason = is_valid_product(slice_.product)
    assert valid, reason


def test_gram_unit_price_rescaled_to_per_kg():
    results = _run()
    bran = next(r for r in results if r.product and r.product["product_id"] == "P5026147")
    assert bran.product["unit_price"] == 19.5
    assert bran.product["unit"] == "kg"


def test_product_with_no_price_is_dropped():
    results = _run()
    dropped = next(r for r in results if r.drop_reason and "P9999999" in r.drop_reason)
    assert dropped.product is None
    assert "price" in dropped.drop_reason.lower()


if __name__ == "__main__":
    test_finds_only_product_tiles_not_other_divs()
    test_normal_product_with_full_fields()
    test_each_item_has_no_unit_price_and_is_not_an_error()
    test_gram_unit_price_rescaled_to_per_kg()
    test_product_with_no_price_is_dropped()
    print("All extract.py tests passed.")
