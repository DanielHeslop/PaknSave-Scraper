"""Watch-category mapping: the given path, name matching in the cached tree, plain
strings, not found, remembered mappings, and de-duplication with the categories
files. No test touches the network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import scraper.category_mapping as category_mapping
import scraper.category_tree as category_tree
import scraper.ingredients as ingredients
import scraper.weekly_combined as weekly_combined
from scraper import woolworths
from scraper.browser import CategoryPage
from scraper.category_mapping import (
    Mapping,
    clean_path,
    extra_foodstuffs_pages,
    find_by_name,
    load_mappings,
    map_watch_categories,
    sync_and_map,
)
from scraper.category_tree import Category, build_foodstuffs_tree, build_woolworths_tree, sitemap_locs
from scraper.ingredients import StoreLists, WatchCategory, fetch_store_lists
from scraper.price_runs import RunResult
from scraper.robots import RobotsRules
from scraper.search_step import SearchOutcome, summary_line

FIXTURES = Path(__file__).parent / "fixtures"


def _foodstuffs(store="paknsave"):
    urls = sitemap_locs((FIXTURES / f"sitemap_categories_{store}.xml").read_text())
    return build_foodstuffs_tree(urls, RobotsRules())[0]


def _woolworths():
    return build_woolworths_tree(json.loads((FIXTURES / "woolworths_shell.json").read_text()), RobotsRules())[0]


def _paths(result):
    return {m.name: m.path for m in result.mapped}


# --- reading the app's watch categories -----------------------------------

class _Resp:
    status = 200

    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_watch_categories_accept_strings_and_objects(monkeypatch):
    payload = {
        "search": [],
        "watch": {
            "categories": [
                "baking",
                {"name": "Oils", "paths": {"paknsave": "pantry/oil--vinegar", "newworld": "", "woolworths": None}},
                {"name": "Bulk foods"},
                "baking",
            ]
        },
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(payload))
    lists = fetch_store_lists("paknsave", "https://example.com/api/ingredient-list")
    assert lists.watch_categories == ["baking", "Oils", "Bulk foods"]
    baking, oils, bulk = lists.watch_category_specs()
    assert baking.paths == {} and baking.path_for("paknsave") == ""
    assert oils.path_for("paknsave") == "pantry/oil--vinegar"
    assert oils.path_for("newworld") == "" and oils.path_for("woolworths") == ""
    assert bulk.paths == {}


# --- finding a category by name -------------------------------------------

def test_exact_name_wins():
    category, how = find_by_name("Bulk Foods", _foodstuffs())
    assert (category.path, how) == ("pantry/bulk-foods", "exact")


def test_normalised_name_ignores_case_plurals_and_ampersands():
    tree = _foodstuffs()
    assert find_by_name("baking supplies and sugar", tree)[0].path == "pantry/baking-supplies--sugar"
    category, how = find_by_name("kitchen cleaning", _woolworths() + [Category("x/kitchen-cleaners", "Kitchen Cleaners", "x")])
    assert (category.path, how) == ("x/kitchen-cleaners", "normalised")


def test_canned_means_tinned():
    category, how = find_by_name("canned foods", _woolworths())
    assert category.path == "pantry/tinned-foods-packets"


def test_closest_by_words_prefers_fewest_extra_words_then_the_shallowest():
    category, how = find_by_name("baking", _foodstuffs())
    assert (category.path, how) == ("pantry/baking-supplies--sugar", "words")
    category, how = find_by_name("oils", _woolworths())
    assert category.path == "pantry/oil-vinegar-condiments/oil"


def test_free_from_categories_do_not_match_the_plain_word():
    tree = [Category("fridge/dairy-free--meat-free", "Dairy Free & Meat Free", "fridge")]
    assert find_by_name("dairy", tree) is None


def test_promo_trees_lose_to_the_real_category():
    tree = _foodstuffs() + [Category("pantry/spices--seasonings", "Spices & Seasonings", "pantry")]
    assert find_by_name("spices", tree)[0].path == "pantry/spices--seasonings"
    assert find_by_name("spices", _foodstuffs())[0].path == "featured/pams/spices--seasonings"  # only copy there is


def test_nothing_matches():
    assert find_by_name("space rockets", _foodstuffs()) is None
    assert find_by_name("", _foodstuffs()) is None
    assert find_by_name("baking", []) is None


# --- mapping --------------------------------------------------------------

def test_given_path_for_this_store_is_used_as_it_is():
    watch = [WatchCategory("Oils", {"paknsave": "pantry/oil--vinegar", "newworld": "pantry/elsewhere"})]
    result = map_watch_categories("paknsave", watch, _foodstuffs())
    assert _paths(result) == {"Oils": "pantry/oil--vinegar"}
    assert result.mapped[0].how == "given"


def test_given_path_is_cleaned_of_the_site_and_prefix():
    assert clean_path("https://www.paknsave.co.nz/shop/category/pantry/bulk-foods?pg=1") == "pantry/bulk-foods"
    assert clean_path("/shop/browse/pantry/baking/") == "pantry/baking"
    assert clean_path("  ") == ""


def test_given_path_is_trusted_even_when_not_in_the_cached_tree():
    result = map_watch_categories("paknsave", [WatchCategory("X", {"paknsave": "brand/new"})], _foodstuffs())
    assert _paths(result) == {"X": "brand/new"}


def test_blank_path_is_found_by_name():
    watch = [WatchCategory("Bulk foods", {"paknsave": "", "woolworths": "pantry/bulk-foods"})]
    result = map_watch_categories("paknsave", watch, _foodstuffs())
    assert _paths(result) == {"Bulk foods": "pantry/bulk-foods"}
    assert result.mapped[0].how == "exact"


def test_other_stores_paths_are_not_used_here():
    watch = [WatchCategory("Baking", {"woolworths": "pantry/baking"})]
    result = map_watch_categories("paknsave", watch, _foodstuffs())
    assert _paths(result) == {"Baking": "pantry/baking-supplies--sugar"}


def test_plain_string_is_found_by_name_at_each_store():
    watch = [WatchCategory("baking"), WatchCategory("canned foods")]
    assert _paths(map_watch_categories("paknsave", watch, _foodstuffs("paknsave"))) == {
        "baking": "pantry/baking-supplies--sugar",
        "canned foods": "pantry/canned-foods--packets",
    }
    assert _paths(map_watch_categories("woolworths", watch, _woolworths())) == {
        "baking": "pantry/baking",
        "canned foods": "pantry/tinned-foods-packets",
    }


def test_not_found_logs_one_line_and_is_skipped(capsys):
    watch = [WatchCategory("space rockets"), WatchCategory("Bulk foods")]
    result = map_watch_categories("newworld", watch, _foodstuffs("newworld"))
    assert result.not_found == ["space rockets"]
    assert _paths(result) == {"Bulk foods": "pantry/bulk-foods"}
    lines = [l for l in capsys.readouterr().out.splitlines() if "not found" in l]
    assert lines == ["watch category 'space rockets' not found at newworld"]


def test_mapping_is_recorded_per_store_and_only_new_ones_count_as_new():
    tree = _foodstuffs()
    watch = [WatchCategory("baking"), WatchCategory("bulk foods")]
    first = map_watch_categories("paknsave", watch, tree)
    assert first.counts() == {"mapped": 2, "new": 2, "not_found": 0}
    saved = load_mappings("paknsave")
    assert saved["baking"]["path"] == "pantry/baking-supplies--sugar"
    assert load_mappings("newworld") == {}

    second = map_watch_categories("paknsave", watch + [WatchCategory("pantry")], tree)
    assert second.counts() == {"mapped": 3, "new": 1, "not_found": 0}
    assert [m.name for m in second.mapped if m.new] == ["pantry"]


def test_remembered_mapping_survives_a_site_that_cannot_be_read():
    watch = [WatchCategory("baking")]
    map_watch_categories("paknsave", watch, _foodstuffs())
    result = map_watch_categories("paknsave", watch, [])
    assert _paths(result) == {"baking": "pantry/baking-supplies--sugar"}
    assert result.newly_mapped == 0


def test_a_category_that_left_the_tree_is_mapped_again():
    watch = [WatchCategory("bulk foods")]
    map_watch_categories("paknsave", watch, [Category("old/bulk-foods", "Bulk Foods", "old")])
    result = map_watch_categories("paknsave", watch, _foodstuffs())
    assert _paths(result) == {"bulk foods": "pantry/bulk-foods"}
    assert result.newly_mapped == 1


def test_dry_run_writes_no_mapping_file():
    map_watch_categories("paknsave", [WatchCategory("baking")], _foodstuffs(), save=False)
    assert load_mappings("paknsave") == {}


# --- de-duplication with the categories files -----------------------------

def _page(path, category="x"):
    return CategoryPage(url=f"https://www.paknsave.co.nz/shop/category/{path}", category=category)


def test_foodstuffs_extras_skip_paths_the_categories_file_already_scrapes():
    existing = [_page("pantry/baking-supplies--sugar", "baking"), _page("pantry/bulk-foods?pg=2")]
    mapped = [
        Mapping("baking", "pantry/baking-supplies--sugar", "words"),
        Mapping("bulk foods", "pantry/bulk-foods", "exact"),
        Mapping("Oils", "pantry/oil--vinegar", "given"),
        Mapping("oil again", "pantry/oil--vinegar", "words"),
    ]
    extra = extra_foodstuffs_pages("paknsave", mapped, existing)
    assert [p.url for p in extra] == ["https://www.paknsave.co.nz/shop/category/pantry/oil--vinegar"]
    assert extra[0].category == "oils" and "oils" in extra[0].covers


def test_woolworths_extras_skip_paths_the_categories_file_already_reads():
    existing = [woolworths.BrowsePage("pantry/baking", "baking")]
    mapped = [
        Mapping("baking", "pantry/baking", "exact"),
        Mapping("bulk foods", "pantry/bulk-foods", "exact"),
    ]
    extra = woolworths.extra_browse_pages(mapped, existing)
    assert [(p.path, p.label) for p in extra] == [("pantry/bulk-foods", "bulk-foods")]


# --- in a run -------------------------------------------------------------

def test_sync_and_map_posts_the_tree_then_maps(monkeypatch):
    monkeypatch.delenv("MAILBOX_URL", raising=False)
    lists = StoreLists(store="paknsave", watch_categories=["baking"], watch_category_entries=[WatchCategory("baking")])
    result = sync_and_map("paknsave", lists, save=True, sync=lambda s, save, guard: category_tree.TreeSync(_foodstuffs(), "site"))
    assert _paths(result) == {"baking": "pantry/baking-supplies--sugar"}


def test_sync_and_map_without_the_app_list_maps_nothing():
    assert sync_and_map("paknsave", None, save=False, sync=lambda s, save, guard: category_tree.TreeSync([], "none")) is None


def test_sync_and_map_never_raises():
    def boom(store, save, guard):
        raise RuntimeError("boom")

    assert sync_and_map("paknsave", StoreLists(store="paknsave"), save=True, sync=boom) is None


def test_summary_line_reports_watch_categories():
    line = summary_line(5, SearchOutcome(), 3, 61, {"mapped": 4, "new": 2, "not_found": 1})
    assert "categories scraped 3, watch categories mapped 4, newly mapped 2, not found 1, matcher" in line
    assert "watch categories" not in summary_line(5, SearchOutcome(), 3, 61)


_STATS = {
    "pages_scraped": 1, "pages_skipped": [], "categories_scraped": 1, "products_found": 1, "products_saved": 1,
    "products_with_unit_price": 1, "products_without_unit_price": 0, "products_dropped": [],
}

_PRODUCT = {
    "product_id": "P1", "name": "Carrots", "category": "vegetables", "size": "kg", "price": 1.99,
    "unit_price": 1.99, "unit": "kg", "scraped_at": "2026-10-03", "supermarket": "Pak'nSave",
}


def test_weekly_run_scrapes_mapped_categories_and_summarises_them(monkeypatch, tmp_path, capsys):
    seen = {}

    async def fake_scrape_all(categories, overrides, headed, site, guard, extra_pages=None):
        seen["extra"] = extra_pages
        return {"products": [_PRODUCT], "stats": _STATS, "scraped_categories": {"vegetables": ("carrot",)}}

    categories_file = tmp_path / "categories.txt"
    categories_file.write_text("https://www.paknsave.co.nz/shop/category/pantry/baking-supplies--sugar category=baking\n")
    lists = StoreLists(
        store="paknsave",
        watch_categories=["baking", "bulk foods", "space rockets"],
        watch_category_entries=[WatchCategory("baking"), WatchCategory("bulk foods"), WatchCategory("space rockets")],
    )
    monkeypatch.setattr(weekly_combined, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(weekly_combined, "fetch_store_lists", lambda store, url: lists)
    monkeypatch.setattr(weekly_combined, "publish_run", lambda products, *a, **kw: RunResult(items=len(products)))
    monkeypatch.setattr(weekly_combined, "report_outcome", lambda store, outcome: None)
    monkeypatch.setattr(category_tree, "fetch_tree", lambda store, guard=None: _foodstuffs())
    args = weekly_combined.parse_args(["--save"])
    args.categories = str(categories_file)
    args.snapshots_dir = tmp_path / "snapshots"
    args.history_path = tmp_path / "history.json"

    assert asyncio.run(weekly_combined.main_async(args)) == 0

    # baking is already in the file, so only bulk foods is added
    assert [p.url for p in seen["extra"]] == ["https://www.paknsave.co.nz/shop/category/pantry/bulk-foods"]
    out = capsys.readouterr().out
    assert "watch category 'space rockets' not found at paknsave" in out
    assert "watch categories mapped 2, newly mapped 2, not found 1" in out.strip().splitlines()[-1]


def test_dry_run_weekly_posts_and_saves_nothing(monkeypatch, tmp_path):
    async def fake_scrape_all(*a, **k):
        return {"products": [], "stats": dict(_STATS, categories_scraped=0), "scraped_categories": {}}

    posted = []
    monkeypatch.setenv("MAILBOX_URL", "https://example-site.netlify.app/api/price-mailbox")
    monkeypatch.setenv("MAILBOX_TOKEN", "t")
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: posted.append(req))
    monkeypatch.setattr(weekly_combined, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(weekly_combined, "fetch_store_lists", lambda s, u: StoreLists(
        store="paknsave", watch_categories=["baking"], watch_category_entries=[WatchCategory("baking")]))
    monkeypatch.setattr(category_tree, "fetch_tree", lambda store, guard=None: _foodstuffs())
    args = weekly_combined.parse_args([])

    assert asyncio.run(weekly_combined.main_async(args)) == 0

    assert posted == []
    assert category_tree.load_cache("paknsave") is None
    assert load_mappings("paknsave") == {}


def test_woolworths_run_browses_mapped_categories(monkeypatch, tmp_path, capsys):
    browsed = []

    def fake_browse(path, max_pages, label):
        browsed.append((path, max_pages, label))
        return [{"product_id": path, "name": "Sugar", "category": label, "size": "1kg", "price": 2.0,
                 "unit_price": 2.0, "unit": "kg", "scraped_at": "2026-11-10", "supermarket": "Woolworths"}], [], 1

    categories_file = tmp_path / "woolworths_categories.txt"
    categories_file.write_text("pantry/baking category=baking\n")
    lists = StoreLists(
        store="woolworths",
        watch_categories=["baking", "bulk foods"],
        watch_category_entries=[WatchCategory("baking"), WatchCategory("Bulk foods", {"woolworths": "pantry/bulk-foods"})],
    )
    monkeypatch.setattr(woolworths, "browse_products", fake_browse)
    monkeypatch.setattr(woolworths, "search_products", lambda t, size: ([], []))
    monkeypatch.setattr(woolworths, "fetch_store_lists", lambda store, url: lists)
    monkeypatch.setattr(woolworths, "publish_run", lambda products, *a, **kw: RunResult(items=len(products)))
    monkeypatch.setattr(woolworths, "report_outcome", lambda store, o: None)
    monkeypatch.setattr(woolworths, "_enrich_sheets", lambda products, save: None)
    monkeypatch.setattr(category_tree, "fetch_tree", lambda store, guard=None: _woolworths())
    monkeypatch.setattr("sys.argv", ["x", "--categories", str(categories_file), "--snapshots-dir", str(tmp_path / "s"),
                                      "--history-path", str(tmp_path / "h.json")])

    assert woolworths.main() == 0

    assert [b[0] for b in browsed] == ["pantry/baking", "pantry/bulk-foods"]
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert "watch categories mapped 2, newly mapped 2, not found 0" in last


def test_post_categories_command_dry_run_maps_without_posting(monkeypatch, capsys):
    from scraper import post_categories

    posted = []
    monkeypatch.setenv("MAILBOX_URL", "https://example-site.netlify.app/api/price-mailbox")
    monkeypatch.setenv("MAILBOX_TOKEN", "t")
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: posted.append(req))
    monkeypatch.setattr(category_tree, "fetch_tree", lambda store, guard=None: _foodstuffs())
    monkeypatch.setattr(
        post_categories, "fetch_store_lists",
        lambda store, url: StoreLists(store=store, watch_category_entries=[WatchCategory("bulk foods")]),
    )

    assert post_categories.main(["--site", "paknsave", "--dry-run", "--map"]) == 0

    assert posted == []
    out = capsys.readouterr().out
    assert "64 categories" in out and "'bulk foods' -> pantry/bulk-foods [exact]" in out


def test_post_categories_command_posts_when_changed(monkeypatch):
    from scraper import post_categories

    posted = []
    monkeypatch.setenv("MAILBOX_URL", "https://example-site.netlify.app/api/price-mailbox")
    monkeypatch.setenv("MAILBOX_TOKEN", "t")
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: posted.append(req) or type("R", (), {
        "status": 200, "__enter__": lambda s: s, "__exit__": lambda s, *a: False})())
    monkeypatch.setattr(category_tree, "fetch_tree", lambda store, guard=None: _foodstuffs())

    assert post_categories.main(["--site", "newworld"]) == 0
    assert post_categories.main(["--site", "newworld"]) == 0
    assert len(posted) == 1


def test_same_name_in_two_aisles_prefers_the_one_whose_aisle_shares_a_word():
    tree = [
        Category("household/cleaning/kitchen-cleaners", "Kitchen Cleaners", "household/cleaning"),
        Category("household/kitchen/kitchen-cleaners", "Kitchen Cleaners", "household/kitchen"),
    ]
    assert find_by_name("kitchen cleaning", tree)[0].path == "household/kitchen/kitchen-cleaners"
