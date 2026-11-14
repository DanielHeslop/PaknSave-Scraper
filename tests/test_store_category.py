"""The store's category path on each item: Pak'nSave / New World (the page it was scraped from,
deepest then first seen), Woolworths (the product's own department / aisle / shelf, matched to the
tree), blank when unknown, and the "category" field of the posted item. No network."""

from __future__ import annotations

import json

import pytest

import scraper.category_mapping as category_mapping
import scraper.store_category as store_category
from scraper import woolworths_transport as wt
from scraper.category_mapping import add_categories_from_labels, label_paths
from scraper.price_runs import with_snapshot_categories
from scraper.publish import build_payload_item
from scraper.search_step import dedupe_products
from scraper.store_category import (
    deeper,
    depth,
    foodstuffs_path_of,
    merge_category,
    woolworths_category,
)

WOOLWORTHS_TREE = [
    {"path": "pantry", "name": "Pantry", "parent": ""},
    {"path": "pantry/baking", "name": "Baking", "parent": "pantry"},
    {"path": "pantry/baking/flour", "name": "Flour", "parent": "pantry/baking"},
    {"path": "pantry/oils-and-vinegars", "name": "Oils & Vinegars", "parent": "pantry"},
    {"path": "fruit-and-veg", "name": "Fruit & Veg", "parent": ""},
    {"path": "fruit-and-veg/fresh-vegetables", "name": "Fresh Vegetables", "parent": "fruit-and-veg"},
]


@pytest.fixture
def tree_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(store_category, "TREE_DIR", str(tmp_path))
    store_category.clear_tree_cache()
    (tmp_path / "woolworths.json").write_text(json.dumps({"categories": WOOLWORTHS_TREE}))
    yield tmp_path
    store_category.clear_tree_cache()


def api_item(**extra) -> dict:
    return {"sku": "123", "name": "Plain Flour 1kg", "price": {"salePrice": 2.5}, "size": {}, **extra}


def parsed(item: dict, browse_path: str | None = None) -> dict:
    product, reason = wt._parse_item(item, "fallback", browse_path)
    assert reason is None
    return product


# --- Pak'nSave / New World ----------------------------------------------------

def test_path_is_the_url_after_shop_category():
    url = "https://www.paknsave.co.nz/shop/category/pantry/oil--vinegar/cooking-oil?pg=2"
    assert foodstuffs_path_of(url) == "pantry/oil--vinegar/cooking-oil"
    assert foodstuffs_path_of("https://www.newworld.co.nz/shop/category/fresh-foods-and-bakery/") == "fresh-foods-and-bakery"


def test_depth_and_deeper():
    assert [depth(p) for p in ("", "a", "a/b", "a/b/c/")] == [0, 1, 2, 3]
    assert deeper("a/b", "a/b/c") == "a/b/c"
    assert deeper("a/b", "x/y") == "a/b"  # same depth: the first seen stays
    assert deeper("", "a") == "a"
    assert deeper("a", "") == "a"


def test_a_product_on_several_pages_keeps_the_deepest_path():
    shallow = {"supermarket": "New World", "product_id": "P1", "name": "Oil", "store_category": "pantry"}
    deep = {"supermarket": "New World", "product_id": "P1", "name": "Oil", "store_category": "pantry/oil/cooking-oil"}
    other = {"supermarket": "New World", "product_id": "P1", "name": "Oil", "store_category": "pantry/baking/oil"}
    out = dedupe_products([shallow, deep, other])
    assert len(out) == 1
    assert out[0]["store_category"] == "pantry/oil/cooking-oil"
    assert shallow["store_category"] == "pantry"  # inputs are not changed


def test_equal_depth_keeps_the_first_seen_page():
    a = {"supermarket": "New World", "product_id": "P1", "store_category": "pantry/oil"}
    b = {"supermarket": "New World", "product_id": "P1", "store_category": "pantry/baking"}
    assert dedupe_products([a, b])[0]["store_category"] == "pantry/oil"
    assert dedupe_products([b, a])[0]["store_category"] == "pantry/baking"


def test_merge_with_no_category_on_either_side_stays_blank():
    kept = {"product_id": "P1"}
    assert merge_category(kept, {"product_id": "P1"}) is kept
    assert merge_category(kept, {"store_category": "a/b"})["store_category"] == "a/b"


# --- Woolworths ---------------------------------------------------------------

def test_woolworths_path_comes_from_the_products_own_api_fields(tree_dir):
    item = api_item(
        departments=[{"id": 1, "name": "Pantry"}], aisles=[{"id": 2, "name": "Baking"}], shelves=[{"id": 3, "name": "Flour"}]
    )
    assert parsed(item)["store_category"] == "pantry/baking/flour"


def test_woolworths_names_match_through_punctuation_and_slugs(tree_dir):
    item = api_item(departments=[{"name": "Fruit & Veg"}], aisles=[{"name": "fresh vegetables"}])
    assert parsed(item)["store_category"] == "fruit-and-veg/fresh-vegetables"
    item = api_item(departments=[{"name": "Pantry"}], aisles=[{"name": "Oils and Vinegars"}])
    assert parsed(item)["store_category"] == "pantry/oils-and-vinegars"


def test_woolworths_stops_at_the_last_level_the_tree_confirms(tree_dir):
    item = api_item(
        departments=[{"name": "Pantry"}], aisles=[{"name": "Baking"}], shelves=[{"name": "Something Not In The Tree"}]
    )
    assert parsed(item)["store_category"] == "pantry/baking"


def test_woolworths_unknown_department_is_blank_not_guessed(tree_dir):
    assert parsed(api_item(departments=[{"name": "Mystery"}], aisles=[{"name": "Baking"}]))["store_category"] == ""
    assert parsed(api_item())["store_category"] == ""
    assert parsed(api_item(departments=[], aisles=None, shelves="x"))["store_category"] == ""


def test_woolworths_with_no_tree_and_no_browse_path_is_blank(tmp_path, monkeypatch):
    monkeypatch.setattr(store_category, "TREE_DIR", str(tmp_path))
    store_category.clear_tree_cache()
    item = api_item(departments=[{"name": "Pantry"}], aisles=[{"name": "Baking"}])
    assert parsed(item)["store_category"] == ""
    assert parsed(item, browse_path="pantry/baking")["store_category"] == "pantry/baking"
    store_category.clear_tree_cache()


def test_woolworths_browse_path_is_used_when_deeper_than_the_own_path(tree_dir):
    item = api_item(departments=[{"name": "Pantry"}])
    assert parsed(item, browse_path="pantry/baking/flour")["store_category"] == "pantry/baking/flour"
    deep_item = api_item(departments=[{"name": "Pantry"}], aisles=[{"name": "Baking"}], shelves=[{"name": "Flour"}])
    assert parsed(deep_item, browse_path="pantry")["store_category"] == "pantry/baking/flour"


def test_woolworths_search_items_use_the_search_apis_category_fields(tree_dir, monkeypatch):
    class Resp:
        def json(self):
            return {"products": {"items": [api_item(departments=[{"name": "Pantry"}], aisles=[{"name": "Baking"}])]}}

    monkeypatch.setattr(wt, "_get_with_retry", lambda *a, **k: Resp())
    products, skipped = wt.search_products("flour")
    assert [p["store_category"] for p in products] == ["pantry/baking"]
    assert products[0]["category"] == "Pantry"  # the existing local label is unchanged


def test_woolworths_category_function_takes_explicit_tree(tree_dir):
    item = api_item(departments=[{"name": "Pantry"}])
    assert woolworths_category(item, tree=store_category.load_tree("woolworths")) == "pantry"


# --- the posted item ----------------------------------------------------------

def _posted(**extra) -> dict:
    product = {
        "product_id": "P1", "name": "Oil", "category": "oil", "size": "1l", "price": 5.0,
        "unit_price": 5.0, "unit": "L", "supermarket": "New World", **extra,
    }
    return build_payload_item(product)


def test_posted_item_carries_the_store_category():
    assert _posted(store_category="pantry/oil--vinegar/cooking-oil")["category"] == "pantry/oil--vinegar/cooking-oil"


def test_posted_item_category_is_blank_when_unknown():
    assert _posted()["category"] == ""
    assert _posted(store_category=None)["category"] == ""


def test_the_local_label_is_not_what_is_posted():
    assert _posted(store_category="pantry/oil")["category"] != "oil"


# --- snapshots written before store_category existed --------------------------

@pytest.fixture
def categories_file(tmp_path):
    path = tmp_path / "categories.txt"
    path.write_text(
        "https://www.newworld.co.nz/shop/category/fruit-and-vegetables/vegetables category=vegetables\n"
        "https://www.newworld.co.nz/shop/category/pantry/oil category=oil\n"
        "https://www.newworld.co.nz/shop/category/pantry/baking category=twice\n"
        "https://www.newworld.co.nz/shop/category/pantry/other category=twice\n"
    )
    return path


def test_labels_name_one_page_each_and_ambiguous_labels_are_dropped(categories_file, monkeypatch, tmp_path):
    monkeypatch.setattr(category_mapping, "load_mappings", lambda store: {})
    paths = label_paths("newworld", categories_file)
    assert paths == {"vegetables": "fruit-and-vegetables/vegetables", "oil": "pantry/oil"}


def test_snapshot_products_get_a_category_from_their_label_only_when_it_is_certain(categories_file, monkeypatch):
    monkeypatch.setattr(category_mapping, "load_mappings", lambda store: {})
    products = [
        {"product_id": "1", "category": "vegetables"},
        {"product_id": "2", "category": "twice"},
        {"product_id": "3", "category": "unheard-of"},
        {"product_id": "4", "category": "oil", "store_category": "pantry/oil/cooking-oil"},
    ]
    out, filled = add_categories_from_labels(products, "newworld", categories_file)
    assert filled == 1
    assert [p.get("store_category", "") for p in out] == ["fruit-and-vegetables/vegetables", "", "", "pantry/oil/cooking-oil"]
    assert "store_category" not in products[0]


def test_woolworths_snapshots_are_left_alone():
    products = [{"product_id": "1", "category": "Pantry"}]
    assert with_snapshot_categories(products, "woolworths") is products


def test_snapshot_with_categories_is_not_touched(monkeypatch):
    monkeypatch.setattr(category_mapping, "add_categories_from_labels", lambda *a, **k: pytest.fail("called"))
    products = [{"product_id": "1", "store_category": "a/b"}]
    assert with_snapshot_categories(products, "paknsave") is products


def test_a_category_is_part_of_the_run_id():
    from scraper.price_runs import make_run_id

    base = [_posted()]
    with_category = [_posted(store_category="pantry/oil")]
    assert make_run_id("newworld", "2026-11-13T08:00:00+13:00", base) != make_run_id(
        "newworld", "2026-11-13T08:00:00+13:00", with_category
    )


# --- real paths before promotional ones ----------------------------------------

from scraper.store_category import PROMO_PATTERNS, PROMO_ROOTS, PROMO_SECOND_LEVELS, better_category, is_promotional  # noqa: E402


@pytest.mark.parametrize(
    "path",
    [
        "featured/pams/spices--seasonings", "featured", "christmas/christmas", "christmas",
        "fish-seafood/3-for-20", "fridge-deli/3-for-12-deli", "fruit-veg/fresh-deals", "fruit-veg/in-season",
        "meat-poultry/2-for-5", "pantry/weekly-specials", "pantry/clearance", "pantry/baking/deals",
        "pantry/baking/special-deals-on-flour", "Featured/Pams",
    ],
)
def test_promotional_paths(path):
    assert is_promotional(path)


@pytest.mark.parametrize(
    "path",
    [
        "", "pantry", "pantry/baking/flour", "fruit-veg/fruit", "fridge-deli/deli-meats-seafood",
        "pantry/spices-seasoning--coatings", "household/ideals", "pantry/for-baking", "pantry/forty-two",
        "pantry/oil--vinegar/cooking-oil",
    ],
)
def test_real_paths_are_not_promotional(path):
    assert not is_promotional(path)


def test_promo_patterns_live_in_one_place():
    assert {"featured", "christmas"} <= PROMO_ROOTS
    assert {"3-for-20", "3-for-12-deli", "fresh-deals", "in-season"} <= PROMO_SECOND_LEVELS
    assert PROMO_PATTERNS


def test_a_real_path_beats_a_deeper_promotional_path():
    assert better_category("featured/pams/spices--seasonings", "pantry/spices-seasoning--coatings") == "pantry/spices-seasoning--coatings"
    assert better_category("pantry/spices-seasoning--coatings", "featured/pams/spices--seasonings") == "pantry/spices-seasoning--coatings"
    assert better_category("fruit-veg/fresh-deals/apples", "fruit-veg") == "fruit-veg"


def test_the_deepest_real_path_wins_then_the_first_seen():
    assert better_category("pantry/baking", "pantry/baking/flour") == "pantry/baking/flour"
    assert better_category("pantry/baking/flour", "pantry/baking") == "pantry/baking/flour"
    assert better_category("pantry/baking", "pantry/oil") == "pantry/baking"
    assert better_category("featured/a", "featured/b/c") == "featured/b/c"  # promotional vs promotional: deepest
    assert better_category("featured/a", "christmas/b") == "featured/a"


def test_a_promotional_path_is_kept_only_when_it_is_alone():
    assert better_category("", "featured/pams") == "featured/pams"
    assert better_category("featured/pams", "") == "featured/pams"
    assert better_category("", "") == ""


def test_dedupe_prefers_real_over_promotional_whatever_the_order():
    def product(path):
        return {"supermarket": "New World", "product_id": "P1", "store_category": path}

    pages = [product("christmas/christmas"), product("pantry/baking-supplies--sugar"), product("featured/pams/baking")]
    assert dedupe_products(pages)[0]["store_category"] == "pantry/baking-supplies--sugar"
    assert dedupe_products(list(reversed(pages)))[0]["store_category"] == "pantry/baking-supplies--sugar"
    assert dedupe_products([pages[0], pages[2]])[0]["store_category"] == "featured/pams/baking"  # alone: deepest promo


def test_woolworths_real_browse_path_beats_a_promotional_own_shelf(tree_dir):
    tree = [
        *WOOLWORTHS_TREE,
        {"path": "fruit-and-veg/fresh-deals", "name": "Fresh Deals", "parent": "fruit-and-veg"},
    ]
    (tree_dir / "woolworths.json").write_text(json.dumps({"categories": tree}))
    store_category.clear_tree_cache()
    item = api_item(departments=[{"name": "Fruit & Veg"}], aisles=[{"name": "Fresh Deals"}])
    assert parsed(item)["store_category"] == "fruit-and-veg/fresh-deals"  # the only path there is
    assert parsed(item, browse_path="fruit-and-veg/fresh-vegetables")["store_category"] == "fruit-and-veg/fresh-vegetables"
    real = api_item(departments=[{"name": "Pantry"}], aisles=[{"name": "Baking"}])
    assert parsed(real, browse_path="pantry/3-for-20")["store_category"] == "pantry/baking"


def test_a_label_prefers_its_real_page_over_a_promotional_one(tmp_path, monkeypatch):
    monkeypatch.setattr(category_mapping, "load_mappings", lambda store: {})
    path = tmp_path / "categories.txt"
    path.write_text(
        "https://www.paknsave.co.nz/shop/category/featured/pams/spices--seasonings category=spices\n"
        "https://www.paknsave.co.nz/shop/category/pantry/spices-seasoning--coatings category=spices\n"
    )
    assert label_paths("paknsave", path) == {"spices": "pantry/spices-seasoning--coatings"}
