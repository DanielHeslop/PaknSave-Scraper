"""Promo and unit-price extraction from Pak'nSave / New World tiles, against
tests/fixtures/promo_tiles.html (hand-built from the audited live markup)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from scraper.extract import extract_product, find_product_tiles
from scraper.item_fields import build_item_fields

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "promo_tiles.html"
_CHROMIUM_OVERRIDE = os.environ.get("PW_TEST_CHROMIUM_PATH")


async def _load():
    async with async_playwright() as p:
        kwargs = {"headless": True}
        if _CHROMIUM_OVERRIDE:
            kwargs["executable_path"] = _CHROMIUM_OVERRIDE
        browser = await p.chromium.launch(**kwargs)
        page = await browser.new_page()
        await page.route("**/fsimg.co.nz/**", lambda route: route.abort())
        await page.goto(FIXTURE_PATH.as_uri())
        out = {}
        for tile in await find_product_tiles(page):
            r = await extract_product(tile, category="test", scraped_at="2026-11-12")
            if r.product:
                out[r.product["product_id"]] = r.product
        await browser.close()
        return out


@pytest.fixture(scope="module")
def products():
    return asyncio.run(_load())


def test_all_tiles_found(products):
    assert len(products) == 7


def test_club_plus_deal_is_member_promo(products):
    p = products["P5022379"]
    assert p["price"] == 10.19
    assert p["pack_price"] == 8.89
    assert p["regular_pack_price"] == 10.19
    assert p["promo_type"] == "member"
    assert p["promo_label"] == "Club Plus Deal"
    assert p["promo_unit_raw"] == "$0.41/100 sheets"
    assert p["unit_raw"] == "$0.47/100 sheets"
    f = build_item_fields(p).payload
    assert f["unit"] == "100sheets"
    assert f["pack_count"] == 8
    assert f["pack_price"] == 8.89 and f["regular_pack_price"] == 10.19
    assert f["price"] == pytest.approx(8.89 / 2160 * 100, abs=0.001)
    assert f["regular_price"] == pytest.approx(10.19 / 2160 * 100, abs=0.001)


def test_multibuy_keeps_regular_pack_price(products):
    p = products["P5212778"]
    assert p["promo_type"] == "multibuy"
    assert p["promo_min_qty"] == 2
    assert p["promo_label"] == "2 for $5.00"
    assert p["pack_price"] == 3.79
    f = build_item_fields(p).payload
    assert f["unit"] == "kg"
    assert f["price"] == f["regular_price"] == 21.1
    assert f["pack_price"] == f["regular_pack_price"] == 3.79


def test_sale_with_limit_text(products):
    p = products["P5253178"]
    assert p["promo_type"] == "sale"
    assert p["pack_price"] == 4.99
    assert p["regular_pack_price"] == 6.49
    assert "Limit 6 assorted" in p["promo_label"]


def test_everyday_low_price_is_not_a_promo(products):
    p = products["P5260093"]
    assert not p.get("promo_type")
    f = build_item_fields(p).payload
    assert f["regular_price"] == f["price"]
    assert "promo_type" not in f
    assert f["unit"] == "100sheets"
    assert f["pack_count"] == 2


def test_bare_per_100_on_tissues_is_sheets(products):
    p = products["P5326473"]
    f = build_item_fields(p).payload
    assert f["unit"] == "100sheets"
    assert f["sheets_per_item"] == 200


def test_plain_product(products):
    p = products["P5268695"]
    assert p["price"] == 12.79
    assert not p.get("promo_type")
    assert p["unit"] == "kg"
    assert p["unit_price"] == 12.8


def test_promo_price_not_below_regular_is_dropped(products):
    p = products["P5099999"]
    assert not p.get("promo_type")
    assert p["price"] == 3.0
    assert p["pack_price"] == 3.0
