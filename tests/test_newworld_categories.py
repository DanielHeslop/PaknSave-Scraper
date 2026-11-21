"""Every New World item posted to the app carries its store category.

Mocked pages only: the real newworld_categories.txt is scraped against fake tiles, the
products are de-duplicated as the weekly job does, and the payload is built the way it is posted.
"""

from __future__ import annotations

import os

import test_watch_pagination as twp
from scraper.browser import parse_categories_file
from scraper.category_mapping import Mapping, extra_foodstuffs_pages
from scraper.publish import build_payload_items
from scraper.search_step import dedupe_products
from scraper.store_category import foodstuffs_path_of
from test_watch_pagination import FakeSite, _ids, _scrape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWWORLD_FILE = os.path.join(ROOT, "newworld_categories.txt")
NW = "https://www.newworld.co.nz/shop/category/"
FULL = twp.FULL

HERBS = "fruit-and-vegetables/fresh-salad--herbs"
SEAFOOD = "meat-poultry-and-seafood/seafood"
WINE_SHELVES = ("beer-wine-and-cider/cask-wine", "beer-wine-and-cider/white-wine", "beer-wine-and-cider/red-wine")


def _file_pages() -> list[str]:
    return [p.url for p in parse_categories_file(NEWWORLD_FILE) if p.page == 1]


def _scrape_file(monkeypatch, tmp_path, pages: dict[str, list[str]]):
    with open(NEWWORLD_FILE) as f:
        return _scrape(monkeypatch, tmp_path, FakeSite(pages), categories=f.read())


def test_every_tile_of_the_shipped_file_carries_its_page_path(monkeypatch, tmp_path):
    urls = _file_pages()
    pages = {url: [f"{n}x{i}" for i in range(3)] for n, url in enumerate(urls)}
    result = _scrape_file(monkeypatch, tmp_path, pages)
    products = result["products"]
    assert products
    paths = {foodstuffs_path_of(u) for u in urls}
    assert all(p["store_category"] in paths for p in products)
    by_path = {p["store_category"] for p in products}
    assert {HERBS, SEAFOOD} <= by_path


def test_the_herb_seafood_and_wine_shelves_each_set_their_path(monkeypatch, tmp_path):
    real = twp.ExtractResult

    def priced(product=None, **kw):
        product.update({"size": "750ml", "price": 9.0, "unit_price": 12.0, "unit": "L"})
        return real(product=product, **kw)

    monkeypatch.setattr(twp, "ExtractResult", priced)
    for path in (HERBS, SEAFOOD, *WINE_SHELVES):
        site = FakeSite({NW + path: _ids("t", 3)})
        result = _scrape(monkeypatch, tmp_path, site, categories=f"{NW}{path} category=shelf\n")
        assert result["products"], path
        assert {p["store_category"] for p in result["products"]} == {path}


def test_shipped_file_lists_the_herb_seafood_and_wine_shelves():
    paths = {foodstuffs_path_of(u) for u in _file_pages()}
    assert {HERBS, SEAFOOD, *WINE_SHELVES} <= paths


def test_pages_read_past_the_limit_keep_the_category(monkeypatch, tmp_path):
    url = NW + "pantry/baking-supplies--sugar"
    site = FakeSite({url: _ids("a", FULL), url + "?pg=2": _ids("b", FULL), url + "?pg=3": _ids("c", FULL), url + "?pg=4": _ids("d", 5)})
    result = _scrape(monkeypatch, tmp_path, site, categories=f"{url} pages=1 category=baking\n")
    assert site.loaded == [url, url + "?pg=2", url + "?pg=3", url + "?pg=4"]
    assert len(result["products"]) == 3 * FULL + 5
    assert {p["store_category"] for p in result["products"]} == {"pantry/baking-supplies--sugar"}


def test_mapped_extra_pages_keep_the_category(monkeypatch, tmp_path):
    path = "pantry/bulk-foods"
    url = NW + path
    extra = extra_foodstuffs_pages("newworld", [Mapping("bulk foods", path, "exact")], [])
    site = FakeSite({url: _ids("a", FULL), url + "?pg=2": _ids("b", 4)})
    result = _scrape(monkeypatch, tmp_path, site, extra=extra)
    assert len(result["products"]) == FULL + 4
    assert {p["store_category"] for p in result["products"]} == {path}


def test_a_product_on_two_pages_keeps_a_category_whichever_copy_comes_first(monkeypatch, tmp_path):
    shallow, deep = NW + "pantry/canned-foods--packets", NW + "pantry/canned-foods--packets/canned-fish"
    promo = NW + "featured/3-for-20"
    site = FakeSite({shallow: ["s1", "dup"], deep: ["dup", "d1"], promo: ["dup", "s1"]})
    categories = f"{promo} category=promo\n{shallow} category=canned\n{deep} category=fish\n"
    result = _scrape(monkeypatch, tmp_path, site, categories=categories)
    assert len(result["products"]) == 6
    deduped = {p["product_id"]: p for p in dedupe_products(result["products"])}
    assert deduped["dup"]["store_category"] == "pantry/canned-foods--packets/canned-fish"
    assert deduped["s1"]["store_category"] == "pantry/canned-foods--packets"
    assert all(p["store_category"] for p in deduped.values())


def test_a_copy_with_no_category_never_wins_over_one_with_a_category():
    blank = {"product_id": "P1", "name": "Thing", "supermarket": "New World", "price": 2.0}
    kept = {**blank, "store_category": "pantry/baking-supplies--sugar"}
    for order in ([blank, kept], [kept, blank]):
        (only,) = dedupe_products(order)
        assert only["store_category"] == "pantry/baking-supplies--sugar"


def test_the_whole_run_posts_a_category_on_every_item(monkeypatch, tmp_path):
    urls = _file_pages()
    pages = {url: _ids(f"n{n}_", 3) for n, url in enumerate(urls)}
    pages[urls[0]] = pages[urls[0]] + pages[urls[1]]  # the same tiles on two pages
    result = _scrape_file(monkeypatch, tmp_path, pages)
    payload = build_payload_items(dedupe_products(result["products"]))
    assert payload
    assert all(item["category"] for item in payload)
