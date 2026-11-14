"""Mapped watch categories are read page by page, like the main lists.

Pak'nSave / New World: scraper.run.scrape_all follows a mapped category's ?pg=
pages (limit MAPPED_FOODSTUFFS_MAX_PAGES) until the last page. Woolworths: the
mapped browse pages get the same page limit as the main list's default.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import scraper.run as run
import scraper.woolworths as woolworths
from scraper import woolworths_transport as transport
from scraper.browser import CategoryPage, PageLoadTimeout
from scraper.category_mapping import MAPPED_FOODSTUFFS_MAX_PAGES, Mapping, extra_foodstuffs_pages
from scraper.extract import ExtractResult

BASE = "https://www.paknsave.co.nz/shop/category/"


class FakeSite:
    """url -> list of product ids on that page (a missing url is an empty page)."""

    def __init__(self, pages: dict[str, list[str]]):
        self.pages = pages
        self.loaded: list[str] = []
        self.pauses = 0
        self.broken: set[str] = set()


def _scrape(monkeypatch, tmp_path, site: FakeSite, categories: str = "", extra=None, guard=None):
    class FakePage:
        url = ""

    class FakeContext:
        async def new_page(self):
            return FakePage()

    class FakeBrowser:
        async def close(self):
            pass

    class FakeChromium:
        async def launch(self, **kw):
            return FakeBrowser()

    class FakeP:
        chromium = FakeChromium()

    @asynccontextmanager
    async def fake_stealth():
        yield FakeP()

    async def fake_context(browser, store_id, domain):
        return FakeContext()

    async def fake_navigate(page, url, log):
        if url in site.broken:
            raise PageLoadTimeout("timed out")
        page.url = url
        site.loaded.append(url)

    async def fake_tiles(page):
        return list(site.pages.get(page.url, []))

    async def fake_extract(tile, category, scraped_at, supermarket):
        return ExtractResult(
            product={
                "product_id": tile, "name": f"Thing {tile}", "category": category, "size": "ea", "price": 2.0,
                "unit_price": None, "unit": None, "scraped_at": scraped_at, "supermarket": supermarket,
            }
        )

    async def fake_pause():
        site.pauses += 1

    monkeypatch.setattr(run, "stealth_playwright", fake_stealth)
    monkeypatch.setattr(run, "new_pinned_context", fake_context)
    monkeypatch.setattr(run, "navigate_and_wait_ready", fake_navigate)
    monkeypatch.setattr(run, "find_product_tiles", fake_tiles)
    monkeypatch.setattr(run, "extract_product", fake_extract)
    monkeypatch.setattr(run, "polite_pause", fake_pause)
    categories_file = tmp_path / "categories.txt"
    categories_file.write_text(categories)
    overrides = tmp_path / "overrides.txt"
    overrides.write_text("")
    return asyncio.run(
        run.scrape_all(str(categories_file), str(overrides), False, "paknsave", guard, extra_pages=extra)
    )


def _ids(prefix: str, count: int, start: int = 0) -> list[str]:
    return [f"{prefix}{n}" for n in range(start, start + count)]


def _mapped(path: str = "pantry/bulk-foods") -> CategoryPage:
    return extra_foodstuffs_pages("paknsave", [Mapping("bulk foods", path, "exact")], [])[0]


def test_a_mapped_category_is_read_until_its_last_short_page(monkeypatch, tmp_path):
    url = BASE + "pantry/bulk-foods"
    site = FakeSite({url: _ids("a", 20), url + "?pg=2": _ids("b", 20), url + "?pg=3": _ids("c", 7)})
    result = _scrape(monkeypatch, tmp_path, site, extra=[_mapped()])
    assert site.loaded == [url, url + "?pg=2", url + "?pg=3"]
    assert len(result["products"]) == 47
    assert site.pauses == 2  # the same pause before every page after the first
    assert result["stats"]["pages_scraped"] == 3


def test_paging_stops_at_the_page_limit(monkeypatch, tmp_path):
    url = BASE + "pantry/bulk-foods"
    pages = {url: _ids("p1-", 20)}
    for n in range(2, 9):
        pages[f"{url}?pg={n}"] = _ids(f"p{n}-", 20)
    site = FakeSite(pages)
    result = _scrape(monkeypatch, tmp_path, site, extra=[_mapped()])
    assert len(site.loaded) == MAPPED_FOODSTUFFS_MAX_PAGES == 5
    assert len(result["products"]) == 100


def test_paging_stops_on_an_empty_page(monkeypatch, tmp_path):
    url = BASE + "pantry/bulk-foods"
    site = FakeSite({url: _ids("a", 20), url + "?pg=2": _ids("b", 20)})
    _scrape(monkeypatch, tmp_path, site, extra=[_mapped()])
    assert site.loaded == [url, url + "?pg=2", url + "?pg=3"]


def test_paging_stops_when_a_page_repeats_the_one_before(monkeypatch, tmp_path):
    url = BASE + "pantry/bulk-foods"
    same = _ids("a", 20)
    site = FakeSite({url: same, url + "?pg=2": same, url + "?pg=3": same})
    result = _scrape(monkeypatch, tmp_path, site, extra=[_mapped()])
    assert site.loaded == [url, url + "?pg=2"]
    assert len(result["products"]) == 40  # the repeat is de-duplicated later, as for every list


def test_a_short_first_page_costs_one_empty_check_of_page_two(monkeypatch, tmp_path):
    # Page one sets the page size, so a short page one can't be known as the last
    # until page two comes back empty. That is the only extra request.
    url = BASE + "pantry/bulk-foods"
    site = FakeSite({url: _ids("a", 9)})
    result = _scrape(monkeypatch, tmp_path, site, extra=[_mapped()])
    assert site.loaded == [url, url + "?pg=2"]
    assert len(result["products"]) == 9


def test_products_the_main_lists_already_scraped_do_not_stop_the_paging(monkeypatch, tmp_path):
    main = BASE + "pantry/spices--seasonings"
    mapped = BASE + "pantry/bulk-foods"
    site = FakeSite({main: _ids("a", 20), mapped: _ids("a", 20), mapped + "?pg=2": _ids("b", 20), mapped + "?pg=3": _ids("c", 3)})
    result = _scrape(monkeypatch, tmp_path, site, categories=f"{main}\n", extra=[_mapped()])
    assert site.loaded == [main, mapped, mapped + "?pg=2", mapped + "?pg=3"]
    assert len(result["products"]) == 63


def test_main_list_pages_are_not_followed_beyond_pages_equals_n(monkeypatch, tmp_path):
    main = BASE + "pantry/spices--seasonings"
    site = FakeSite({main: _ids("a", 20), main + "?pg=2": _ids("b", 20), main + "?pg=3": _ids("c", 20)})
    _scrape(monkeypatch, tmp_path, site, categories=f"{main} pages=2\n")
    assert site.loaded == [main, main + "?pg=2"]


def test_a_failed_page_load_ends_that_categorys_paging_but_not_the_run(monkeypatch, tmp_path):
    first = BASE + "pantry/bulk-foods"
    second = BASE + "pantry/stock"
    site = FakeSite({first: _ids("a", 20), first + "?pg=2": _ids("b", 20), second: _ids("s", 4)})
    site.broken.add(first + "?pg=2")
    result = _scrape(monkeypatch, tmp_path, site, extra=[_mapped(), _mapped("pantry/stock")])
    assert site.loaded == [first, second, second + "?pg=2"]
    assert result["stats"]["pages_skipped"] == [first + "?pg=2"]
    assert len(result["products"]) == 24


def test_robots_txt_is_checked_for_every_page(monkeypatch, tmp_path):
    from scraper.robots import RobotsGuard

    url = BASE + "pantry/bulk-foods"
    site = FakeSite({url: _ids("a", 20), url + "?pg=2": _ids("b", 20), url + "?pg=3": _ids("c", 20)})
    guard = RobotsGuard()
    monkeypatch.setattr(guard, "allows", lambda u: "pg=2" not in u)
    result = _scrape(monkeypatch, tmp_path, site, extra=[_mapped()], guard=guard)
    assert site.loaded == [url]
    assert result["stats"]["robots_skipped"] == [url + "?pg=2"]


def test_mapped_pages_are_made_pageable_and_skip_the_main_lists_paths():
    existing = [CategoryPage(url=BASE + "pantry/bulk-foods?pg=2", category="bulk")]
    extra = extra_foodstuffs_pages(
        "paknsave", [Mapping("bulk foods", "pantry/bulk-foods", "exact"), Mapping("stock", "pantry/stock", "exact")], existing
    )
    assert [p.url for p in extra] == [BASE + "pantry/stock"]
    assert extra[0].max_pages == MAPPED_FOODSTUFFS_MAX_PAGES and extra[0].page == 1


# --- Woolworths ---------------------------------------------------------------

def _woolworths_reply(monkeypatch, total: int, calls: list):
    class Resp:
        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    def fake_get(session, url, params=None):
        page = int(dict(params)["page"])
        calls.append(page)
        count = max(0, min(transport.BROWSE_PAGE_SIZE, total - (page - 1) * transport.BROWSE_PAGE_SIZE))
        items = [
            {"sku": f"{page}-{n}", "name": f"thing {page} {n}", "price": {"salePrice": 1.0}, "size": {}} for n in range(count)
        ]
        return Resp({"products": {"items": items, "totalItems": total}})

    monkeypatch.setattr(transport, "_get_with_retry", fake_get)
    monkeypatch.setattr(transport, "_new_session", lambda: object())


def test_woolworths_mapped_categories_get_the_main_lists_page_limit():
    extra = woolworths.extra_browse_pages([Mapping("oils", "pantry/oils", "exact")], [])
    assert extra[0].max_pages == woolworths.DEFAULT_BROWSE_MAX_PAGES == 5


def test_woolworths_mapped_category_is_read_page_by_page_to_the_end(monkeypatch):
    calls: list[int] = []
    _woolworths_reply(monkeypatch, 300, calls)  # 120 + 120 + 60
    page = woolworths.extra_browse_pages([Mapping("oils", "pantry/oils", "exact")], [])[0]
    result = woolworths.browse_all([page])
    assert calls == [1, 2, 3]
    assert len(result["products"]) == 300


def test_woolworths_mapped_category_stops_at_the_page_limit(monkeypatch):
    calls: list[int] = []
    _woolworths_reply(monkeypatch, 2000, calls)
    page = woolworths.extra_browse_pages([Mapping("oils", "pantry/oils", "exact")], [])[0]
    result = woolworths.browse_all([page])
    assert calls == [1, 2, 3, 4, 5]
    assert len(result["products"]) == 600


def test_woolworths_mapped_products_are_deduplicated_with_the_main_lists(monkeypatch):
    calls: list[int] = []
    _woolworths_reply(monkeypatch, 100, calls)
    main = woolworths.BrowsePage("pantry/oils", "oils")
    mapped = woolworths.BrowsePage("pantry/oils-2", "oils-2", (), woolworths.DEFAULT_BROWSE_MAX_PAGES)
    result = woolworths.browse_all([main, mapped])
    assert len(result["products"]) == 100  # both paths returned the same 100 skus
