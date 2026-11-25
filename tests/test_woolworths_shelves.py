"""Woolworths reads its main grocery shelves in full: the shelf list comes from the posted category
tree (`@aisles` rules in woolworths_categories.txt), promotional shelves are left out, products are
deduplicated against the search results, and a run cut off part way carries on across the new shelves."""

from __future__ import annotations

import json
import os
import sys

import pytest

from scraper import category_mapping, category_tree, checkpoint, woolworths, woolworths_shelves
from scraper.category_tree import Category, categories_from_json
from scraper.ingredients import StoreLists
from scraper.price_runs import RunResult
from scraper.store_category import is_promotional, woolworths_category
from tests.test_woolworths_flow import _p

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "woolworths_tree_slice.json")
SHIPPED = woolworths.DEFAULT_CATEGORIES_PATH


@pytest.fixture(scope="module")
def tree() -> list[Category]:
    with open(FIXTURE) as f:
        return categories_from_json(json.load(f)["categories"])


@pytest.fixture
def tree_paths(tree):
    return [c.path for c in tree]


def _planned(tree_paths, file=SHIPPED):
    pages, notes = woolworths.planned_pages(file, tree_paths, woolworths.parse_categories_file(file))
    return pages, notes


def _rules_file(tmp_path, *lines):
    path = tmp_path / "c.txt"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


# --- the shelf list built from the tree --------------------------------------------------------


def test_the_shipped_rules_name_the_main_departments():
    rules = {r.department: r for r in woolworths_shelves.parse_rules(SHIPPED)}
    assert set(rules) == {"fruit-veg", "meat-poultry", "fish-seafood", "fridge-deli", "bakery", "pantry", "frozen", "household", "health-body"}
    assert rules["fruit-veg"].aisles == ("fruit", "vegetables", "fresh-salad-herbs", "prepared-fruit-veg")
    assert rules["pantry"].aisles is None
    assert rules["household"].aisles == ("cleaning", "laundry", "kitchen", "bathroom")
    assert rules["health-body"].aisles == ("dental-oral-care", "hair-care", "bath-shower-soap")


def test_shelf_list_is_the_named_second_level_shelves_of_the_tree(tree_paths):
    pages, notes = _planned(tree_paths)
    paths = [p.path for p in pages]
    assert len(paths) == len(set(paths))

    for wanted in (
        "fruit-veg/fruit", "fruit-veg/vegetables", "fruit-veg/fresh-salad-herbs", "fruit-veg/prepared-fruit-veg",
        "meat-poultry/beef", "meat-poultry/sausages", "fish-seafood/fish", "fish-seafood/salmon",
        "fridge-deli/cheese", "fridge-deli/cream-custard", "fridge-deli/eggs-butter-spreads", "fridge-deli/milk",
        "fridge-deli/yoghurt-desserts", "fridge-deli/deli-meats-seafood", "fridge-deli/dips-hummus-nibbles",
        "fridge-deli/pasta-pizza-pastry", "bakery/sliced-packaged-bread", "pantry/baking", "pantry/snacks-sweets",
        "frozen/frozen-vegetables", "frozen/frozen-meat", "frozen/frozen-seafood", "frozen/frozen-fruit-drink",
        "frozen/pizza-pastry-bread", "household/cleaning", "household/laundry", "household/kitchen",
        "household/bathroom", "health-body/dental-oral-care", "health-body/hair-care", "health-body/bath-shower-soap",
    ):
        assert wanted in paths, wanted

    pantry_aisles = {p for p in tree_paths if p.startswith("pantry/") and p.count("/") == 1}
    assert pantry_aisles <= set(paths)
    assert 60 <= len(paths) <= 70  # about 65 whole categories, like Pak'nSave and New World
    assert any("Woolworths shelves" in n and "from the category tree" in n for n in notes)


def test_aisles_that_are_not_named_are_not_read(tree_paths):
    paths = {p.path for p in _planned(tree_paths)[0]}
    for left_out in (
        "fruit-veg/floral", "fruit-veg/organic", "fridge-deli/juice-drinks", "fridge-deli/prepared-meals-sides",
        "frozen/ice-cream-sorbet", "household/garden-garage", "health-body/vitamins-supplements",
        "drinks/anything", "pet/anything", "baby-child/anything",
    ):
        assert left_out not in paths
    assert {p for p in paths if p.startswith("beer-wine/")} == {"beer-wine/cask-wine", "beer-wine/white-wine", "beer-wine/red-wine"}


def test_toilet_paper_and_paper_towels_are_inside_the_household_aisles_read(tree_paths):
    paths = {p.path for p in _planned(tree_paths)[0]}
    assert {"household/bathroom", "household/kitchen"} <= paths
    assert "household/bathroom/toilet-paper" in tree_paths and "household/kitchen/paper-towels" in tree_paths


def test_every_page_has_a_unique_label_and_a_page_limit(tree_paths):
    pages, _ = _planned(tree_paths)
    labels = [p.label for p in pages]
    assert len(labels) == len(set(labels))
    assert all(p.max_pages >= 1 for p in pages)


def test_a_path_the_tree_no_longer_has_is_logged_and_skipped(tmp_path, tree_paths):
    file = _rules_file(tmp_path, "@aisles fruit-veg fruit,not-an-aisle", "@aisles gone-dept *")
    pages, notes = _planned(tree_paths, file)
    assert [p.path for p in pages] == ["fruit-veg/fruit"]
    assert any("fruit-veg/not-an-aisle" in n and "gone-dept" in n for n in notes)


def test_without_a_tree_the_rules_are_not_applied_and_the_listed_shelves_stay(tmp_path):
    file = _rules_file(tmp_path, "pantry/baking", "@aisles pantry *")
    pages, notes = woolworths.planned_pages(file, [], woolworths.parse_categories_file(file))
    assert [p.path for p in pages] == ["pantry/baking"]
    assert any("no category tree" in n for n in notes)


def test_rule_lines_are_not_browse_paths(tmp_path):
    file = _rules_file(tmp_path, "pantry/baking pages=2", "@aisles pantry *", "# note")
    assert [p.path for p in woolworths.parse_categories_file(file)] == ["pantry/baking"]
    assert category_mapping.woolworths_file_paths(file) == ["pantry/baking"]


def test_a_pages_option_on_a_rule_is_the_soft_page_limit(tmp_path, tree_paths):
    pages, _ = _planned(tree_paths, _rules_file(tmp_path, "@aisles fruit-veg fruit pages=7"))
    assert [(p.path, p.max_pages) for p in pages] == [("fruit-veg/fruit", 7)]


def test_listed_shelves_inside_an_aisle_are_folded_in_and_keep_their_covers(tree_paths):
    pages, notes = _planned(tree_paths)
    by_path = {p.path: p for p in pages}
    assert "pantry/herbs-spices-stock/stock" not in by_path
    assert {"stock", "broth", "bouillon"} <= set(by_path["pantry/herbs-spices-stock"].covers)
    assert "household/kitchen/kitchen-cleaners" not in by_path
    assert {"cleaner", "spray"} <= set(by_path["household/kitchen"].covers)
    # a listed aisle keeps its own label and covers, and is read once
    assert by_path["pantry/baking"].label == "baking" and "flour" in by_path["pantry/baking"].covers
    assert by_path["fruit-veg/fresh-salad-herbs"].label == "fresh-herbs"
    assert "folded into them" in " ".join(notes)


def test_cask_and_cheapest_wine_shelves_are_still_read_as_listed(tree_paths):
    by_path = {p.path: p for p in _planned(tree_paths)[0]}
    for wine in ("beer-wine/cask-wine", "beer-wine/white-wine", "beer-wine/red-wine"):
        assert wine in by_path


# --- promotional shelves ----------------------------------------------------------------------


def test_promotional_aisles_are_never_read(tree_paths):
    pages, notes = _planned(tree_paths)
    assert not [p.path for p in pages if is_promotional(p.path)]
    assert "meat-poultry/3-for-20" in tree_paths and "meat-poultry/3-for-20" not in {p.path for p in pages}
    assert "fish-seafood/3-for-20" not in {p.path for p in pages}
    assert any("promotional" in n and "meat-poultry/3-for-20" in n for n in notes)


def test_a_promotional_aisle_named_outright_is_skipped_too(tmp_path, tree_paths):
    assert {"fruit-veg/fresh-deals", "fruit-veg/in-season"} <= set(tree_paths)
    file = _rules_file(tmp_path, "@aisles fruit-veg fruit,fresh-deals,in-season")
    pages, notes = _planned(tree_paths, file)
    assert [p.path for p in pages] == ["fruit-veg/fruit"]
    assert any("fresh-deals" in n and "in-season" in n for n in notes)


# --- dedupe -----------------------------------------------------------------------------------


def test_drop_covered_keeps_the_first_of_a_repeat_and_drops_inner_shelves():
    keep, dropped = woolworths_shelves.drop_covered(["a/b/c", "a/b", "x/y", "x/y", "x/yy"])
    assert keep == [1, 2, 4]
    assert ("a/b/c", "a/b") in dropped and ("x/y", "x/y") in dropped


def test_browse_all_reads_a_product_on_two_shelves_once_with_the_deeper_category(monkeypatch):
    def fake(path, max_pages, label, stat=None):
        deeper = "fruit-veg/vegetables/pumpkin" if path == "fruit-veg/vegetables" else "fruit-veg"
        return [{**_p("p1", "Pumpkin"), "store_category": deeper}], [], 1

    monkeypatch.setattr(woolworths, "browse_products", fake)
    pages = [woolworths.BrowsePage("fruit-veg", "fruit-veg"), woolworths.BrowsePage("fruit-veg/vegetables", "vegetables")]
    result = woolworths.browse_all(pages)
    assert [p["product_id"] for p in result["products"]] == ["p1"]
    assert result["products"][0]["store_category"] == "fruit-veg/vegetables/pumpkin"


def test_a_browsed_product_is_a_shelf_category_even_when_the_item_has_no_path_of_its_own():
    assert woolworths_category({}, "fridge-deli/cream-custard") == "fridge-deli/cream-custard"


def _run(monkeypatch, tmp_path, tree, *, browse, search, terms=("leek",), extra_args=("--save",)):
    published, history_added = [], []
    monkeypatch.setattr(category_tree, "fetch_tree", lambda store, guard=None: tree)
    lists = StoreLists(store="woolworths", search_terms=list(terms), watch_terms=[], skip=frozenset())
    monkeypatch.setattr(woolworths, "fetch_store_lists", lambda store, url: lists)
    monkeypatch.setattr(woolworths, "browse_products", browse)
    monkeypatch.setattr(woolworths, "search_products", search)
    monkeypatch.setattr(woolworths, "publish_run", lambda products, *a, **kw: published.append(products) or RunResult(items=len(products)))
    monkeypatch.setattr(woolworths, "report_outcome", lambda store, o: None)
    real_update = woolworths.update_price_history
    monkeypatch.setattr(
        woolworths, "update_price_history",
        lambda products, path: history_added.append([p["product_id"] for p in products]) or real_update(products, path),
    )
    cats = tmp_path / "cats.txt"
    cats.write_text("@aisles fruit-veg fruit,vegetables\n@aisles fridge-deli cream-custard\n")
    monkeypatch.setattr(sys, "argv", ["x", "--categories", str(cats), "--snapshots-dir", str(tmp_path / "snap"),
                                      "--history-path", str(tmp_path / "h.json"), *extra_args])
    return woolworths.main(), published, history_added


def _browse_by_shelf(loaded=None, cut_at=None):
    class PowerCut(BaseException):
        pass

    def fake(path, max_pages, label, stat=None):
        if path == cut_at:
            raise PowerCut
        if loaded is not None:
            loaded.append(path)
        n = path.replace("/", "-")
        name = "Leeks Bunch" if path.endswith("vegetables") else f"Item of {path}"
        return [{**_p(f"b-{n}", name), "store_category": path}], [], 1

    return fake, PowerCut


def test_search_results_already_on_a_shelf_are_not_published_or_recorded_twice(monkeypatch, tmp_path, tree):
    browse, _ = _browse_by_shelf()
    shelf_item = {**_p("b-fruit-veg-vegetables", "Leeks Bunch"), "store_category": "fruit-veg/vegetables/leeks"}

    def search(term, size):
        return [shelf_item, _p("s-leek", "Leeks Bunch")], []

    code, published, history_added = _run(monkeypatch, tmp_path, tree, browse=browse, search=search)
    assert code == 0
    ids = sorted(p["product_id"] for p in published[0])
    assert ids == ["b-fridge-deli-cream-custard", "b-fruit-veg-fruit", "b-fruit-veg-vegetables", "s-leek"]
    kept = {p["product_id"]: p for p in published[0]}
    assert kept["b-fruit-veg-vegetables"]["store_category"] == "fruit-veg/vegetables/leeks"
    assert history_added[-1] == ["s-leek"]


def test_the_run_browses_the_shelves_from_the_tree_and_the_search_terms_too(monkeypatch, tmp_path, tree, capsys):
    loaded = []
    browse, _ = _browse_by_shelf(loaded)
    searched = []
    code, published, _ = _run(
        monkeypatch, tmp_path, tree, browse=browse,
        search=lambda term, size: searched.append(term) or ([_p(f"s-{term}", term)], []), terms=("leek", "cumin seeds"),
    )
    assert code == 0
    assert loaded == ["fruit-veg/fruit", "fruit-veg/vegetables", "fridge-deli/cream-custard"]
    assert searched == ["leek", "cumin seeds"]
    assert len(published[0]) == 5
    assert "Woolworths shelves: 3 aisle(s) from the category tree" in capsys.readouterr().out


def test_tree_out_is_filled_even_when_the_app_list_is_missing(tree):
    out: list = []
    result = category_mapping.sync_and_map(
        "woolworths", None, save=False, sync=lambda store, save, guard=None: category_tree.TreeSync(tree, "cache"), tree_out=out
    )
    assert result is None and [c.path for c in out] == [c.path for c in tree]


# --- checkpoint resume across the new shelves ---------------------------------------------------


def test_a_cut_off_run_carries_on_after_the_last_finished_new_shelf(monkeypatch, tmp_path, tree):
    browse, PowerCut = _browse_by_shelf(cut_at="fruit-veg/vegetables")
    search = lambda term, size: ([_p("s-leek", "Leeks Bunch")], [])  # noqa: E731
    with pytest.raises(PowerCut):
        _run(monkeypatch, tmp_path, tree, browse=browse, search=search)
    saved = checkpoint.read_today("woolworths")["sections"]["browse"]["state"]
    assert saved["done"] == 1 and [p["product_id"] for p in saved["products"]] == ["b-fruit-veg-fruit"]

    loaded = []
    browse, _ = _browse_by_shelf(loaded)
    code, published, _ = _run(monkeypatch, tmp_path, tree, browse=browse, search=search)
    assert code == 0
    assert loaded == ["fruit-veg/vegetables", "fridge-deli/cream-custard"]
    assert sorted(p["product_id"] for p in published[0]) == [
        "b-fridge-deli-cream-custard", "b-fruit-veg-fruit", "b-fruit-veg-vegetables", "s-leek"]
    assert not checkpoint.exists_today("woolworths")


def test_a_changed_shelf_list_does_not_resume_from_an_old_checkpoint(monkeypatch, tmp_path, tree):
    browse, PowerCut = _browse_by_shelf(cut_at="fruit-veg/vegetables")
    search = lambda term, size: ([], [])  # noqa: E731
    with pytest.raises(PowerCut):
        _run(monkeypatch, tmp_path, tree, browse=browse, search=search)

    without_vegetables = [c for c in tree if not c.path.startswith("fruit-veg/vegetables")]
    loaded = []
    browse, _ = _browse_by_shelf(loaded)
    code, _, _ = _run(monkeypatch, tmp_path, without_vegetables, browse=browse, search=search)
    assert code == 0
    assert loaded == ["fruit-veg/fruit", "fridge-deli/cream-custard"]  # started again from the first shelf


def test_the_shelf_list_is_part_of_the_checkpoint_signature(tree_paths):
    pages, _ = _planned(tree_paths)
    assert woolworths.browse_signature(pages) != woolworths.browse_signature(pages[:-1])
    assert woolworths.browse_signature(pages) == woolworths.browse_signature(list(pages))


# --- the dry-run listing ------------------------------------------------------------------------


def test_expected_pages_follow_the_last_run_and_the_remembered_page_counts(monkeypatch, tree_paths):
    monkeypatch.setattr("scraper.page_limits.load_page_counts", lambda store: {"fruit-veg/vegetables": 7})
    result = woolworths_shelves.expect(
        ["fruit-veg/fruit", "fruit-veg/vegetables"], tree_paths, {"fruit-veg/fruit": 450}
    )
    by = {e.path: e for e in result}
    assert (by["fruit-veg/fruit"].items, by["fruit-veg/fruit"].pages, by["fruit-veg/fruit"].basis) == (450, 4, "last run")
    assert by["fruit-veg/vegetables"].pages == 7 and by["fruit-veg/vegetables"].basis == "estimate"
    assert by["fruit-veg/vegetables"].items == 570  # 19 shelves at the 30 a shelf the known aisle showed


def test_the_dry_run_listing_prints_the_shelves_without_a_request(monkeypatch, tree, capsys):
    category_tree.save_cache("woolworths", tree, "2026-11-24T00:00:00+00:00", None)
    monkeypatch.setattr(woolworths, "browse_products", lambda *a, **k: pytest.fail("no request in a dry run"))
    assert woolworths_shelves.main([]) == 0
    out = capsys.readouterr().out
    assert "fridge-deli/cream-custard" in out and "fruit-veg/vegetables" in out
    table = [line for line in out.splitlines() if line[:4].strip().isdigit()]
    assert table and not any("3-for-20" in line for line in table)
    assert "page requests" in out and "minutes" in out
