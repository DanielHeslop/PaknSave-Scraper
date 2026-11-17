"""Fresh herbs, in-shell mussels and cheap cooking wine.

The cask wine shelf is kept whole; on the white-wine and red-wine shelves only the cheapest
five per shelf (by price per litre) are kept. Sparkling, fortified, flavoured, dessert,
moscato, non-alcoholic and gift-pack wines never count.
"""

from __future__ import annotations

import os

import pytest
import test_watch_pagination as twp
from scraper import cooking_wine, woolworths
from scraper.browser import parse_categories_file
from scraper.cooking_wine import cheapest_wines, excluded_reason, price_per_litre, wine_colour
from scraper.extract import ExtractResult
from scraper.matching import is_local_match, match_products
from test_watch_pagination import BASE, FakeSite, _scrape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINE = BASE + "beer-wine-and-cider/"


def _wine(pid, name, price, size="750ml", per_litre=None, **extra):
    return {
        "product_id": pid, "name": name, "size": size, "price": price,
        "unit_price": per_litre, "unit": "L" if per_litre is not None else None, **extra,
    }


# --- shelves ----------------------------------------------------------------------

@pytest.mark.parametrize("path,colour", [
    ("beer-wine-and-cider/white-wine", "white"),
    ("beer-wine-and-cider/red-wine", "red"),
    ("beer-wine/white-wine", "white"),
    ("beer-wine/red-wine/shiraz", "red"),
    ("beer-wine-and-cider/cask-wine", None),
    ("beer-wine/cask-wine/cask-wine-2l", None),
    ("beer-wine-and-cider/sparkling-wine", None),
    ("pantry/white-wine", None),
    ("", None),
])
def test_only_the_white_and_red_shelves_have_a_colour(path, colour):
    assert wine_colour(path) == colour


def test_the_cask_shelf_is_named():
    assert cooking_wine.is_cask_shelf("beer-wine-and-cider/cask-wine")
    assert cooking_wine.is_cask_shelf("beer-wine/cask-wine/cask-wine-3l")
    assert not cooking_wine.is_cask_shelf("beer-wine/red-wine")


# --- exclusions -------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Lindauer Brut Sparkling Wine 750ml", "Moscato Dolce 750ml", "Seppelt Fortified Tawny Port 750ml",
    "Cooper's Cream Sherry", "Alcohol Free Chardonnay 750ml", "Non-Alcoholic Red Wine", "Villa Maria 0% Sauvignon Blanc",
    "Peach Flavoured Wine 750ml", "Late Harvest Riesling 375ml", "Wine Lovers Gift Pack 2 x 750ml",
    "Prosecco 750ml", "Sangria 1.5L", "Dessert Wine 375ml",
])
def test_unwanted_wines_are_ruled_out(name):
    assert excluded_reason(name)


@pytest.mark.parametrize("name", [
    "Oyster Bay Sauvignon Blanc 750ml", "Church Road Shiraz", "Cask Dry White Wine 4L", "Montana Pinot Noir 750ml",
])
def test_plain_table_wines_are_not_ruled_out(name):
    assert excluded_reason(name) is None


# --- price per litre ---------------------------------------------------------------

def test_the_stores_own_unit_price_is_used_when_it_is_per_litre():
    assert price_per_litre(_wine("1", "Wine", 9.99, per_litre=13.32)) == 13.32


def test_a_bottle_without_a_unit_price_is_worked_out_from_its_size():
    assert price_per_litre(_wine("1", "Wine", 9.0, size="750ml")) == 12.0
    assert price_per_litre(_wine("2", "Cask", 12.0, size="2L")) == 6.0


def test_a_member_price_is_never_used_for_ranking():
    club = _wine("1", "Wine", 5.0, size="750ml", per_litre=6.67, promo_type="member",
                 regular_pack_price=9.0, unit_raw="$12.00/1L")
    assert price_per_litre(club) == 12.0
    club["unit_raw"] = ""
    assert price_per_litre(club) == 12.0  # from the regular pack price and the size


def test_a_public_sale_price_is_used():
    sale = _wine("1", "Wine", 6.0, size="750ml", per_litre=8.0, promo_type="sale", regular_pack_price=9.0)
    assert price_per_litre(sale) == 8.0


def test_no_size_and_no_unit_price_is_unknown():
    assert price_per_litre(_wine("1", "Wine", 9.0, size="ea")) is None


# --- cheapest five --------------------------------------------------------------------

def _shelf():
    return [
        _wine("a", "Brand A Sauvignon Blanc", 12.0, per_litre=16.0),
        _wine("b", "Brand B Chardonnay", 9.0, per_litre=12.0),
        _wine("c", "Brand C Pinot Gris", 8.0, per_litre=10.67),
        _wine("d", "Cheap Sparkling Brut", 5.0, per_litre=6.67),
        _wine("e", "Brand E Riesling", 10.0, per_litre=13.33),
        _wine("f", "Brand F Alcohol Free White", 4.0, per_litre=5.33),
        _wine("g", "Brand G Semillon", 7.5, per_litre=10.0),
        _wine("h", "Brand H Gift Pack", 6.0, per_litre=8.0),
        _wine("i", "Brand I Chenin", 11.0, per_litre=14.67),
        _wine("j", "Brand J Unknown", 9.0, size="ea"),
        _wine("k", "Brand K Verdelho", 13.0, per_litre=17.33),
    ]


def test_the_five_cheapest_by_price_per_litre_are_kept_in_order():
    kept, notes, report = cheapest_wines(_shelf())
    assert [p["product_id"] for p in kept] == ["g", "c", "b", "e", "i"]
    assert [round(r["per_litre"], 2) for r in report] == [10.0, 10.67, 12.0, 13.33, 14.67]
    assert any("Sparkling" in n for n in notes) and any("Alcohol Free" in n for n in notes)
    assert any("Gift Pack" in n for n in notes) and any("no price per litre" in n for n in notes)


def test_a_tie_goes_to_the_lower_shelf_price_then_the_name():
    shelf = [_wine("x", "B Wine", 9.0, per_litre=12.0), _wine("y", "A Wine", 9.0, per_litre=12.0),
             _wine("z", "Big Bottle", 18.0, size="1.5L", per_litre=12.0)]
    kept, _, _ = cheapest_wines(shelf, count=3)
    assert [p["product_id"] for p in kept] == ["y", "x", "z"]


def test_a_wine_seen_twice_counts_once():
    shelf = [_wine("a", "Wine A", 9.0, per_litre=12.0)] * 3 + [_wine("b", "Wine B", 10.0, per_litre=13.33)]
    kept, _, _ = cheapest_wines(shelf, count=2)
    assert [p["product_id"] for p in kept] == ["a", "b"]


def test_a_short_shelf_keeps_what_it_has():
    kept, _, _ = cheapest_wines([_wine("a", "Wine A", 9.0, per_litre=12.0)])
    assert len(kept) == 1


def test_select_for_shelf_leaves_other_shelves_alone():
    shelf = _shelf()
    assert cooking_wine.select_for_shelf("beer-wine-and-cider/cask-wine", shelf) == (shelf, 0)
    assert cooking_wine.select_for_shelf("pantry/baking", shelf) == (shelf, 0)
    kept, set_aside = cooking_wine.select_for_shelf("beer-wine-and-cider/white-wine", shelf)
    assert len(kept) == 5 and set_aside == len(shelf) - 5


# --- Pak'nSave / New World run ----------------------------------------------------------

def _scrape_wines(monkeypatch, tmp_path, pages, categories):
    def result(product=None, **kw):
        product.update(WINES[product["product_id"]])
        return ExtractResult(product=product, **kw)

    monkeypatch.setattr(twp, "ExtractResult", result)
    site = FakeSite(pages)
    return site, _scrape(monkeypatch, tmp_path, site, categories=categories)


WINES = {
    **{f"w{n}": {"name": f"White {n} Sauvignon Blanc", "size": "750ml", "price": 20.0 - n, "unit_price": (20.0 - n) / 0.75, "unit": "L"}
       for n in range(1, 9)},
    "wsp": {"name": "White Sparkling Brut", "size": "750ml", "price": 3.0, "unit_price": 4.0, "unit": "L"},
    **{f"r{n}": {"name": f"Red {n} Merlot", "size": "750ml", "price": 30.0 - n, "unit_price": (30.0 - n) / 0.75, "unit": "L"}
       for n in range(1, 4)},
    "c1": {"name": "Cask Dry White 2L", "size": "2L", "price": 14.0, "unit_price": 7.0, "unit": "L"},
    "c2": {"name": "Cask Sparkling Red 2L", "size": "2L", "price": 30.0, "unit_price": 15.0, "unit": "L"},
}


def test_a_run_keeps_every_cask_and_only_the_cheapest_five_of_each_colour(monkeypatch, tmp_path):
    pages = {
        WINE + "cask-wine": ["c1", "c2"],
        WINE + "white-wine": [f"w{n}" for n in range(1, 9)] + ["wsp"],
        WINE + "red-wine": ["r1", "r2", "r3"],
    }
    categories = (
        f"{WINE}cask-wine category=cask-wine\n{WINE}white-wine category=white-wine\n{WINE}red-wine category=red-wine\n"
    )
    _, result = _scrape_wines(monkeypatch, tmp_path, pages, categories)
    by_id = {p["product_id"]: p for p in result["products"]}
    assert {"c1", "c2"} <= set(by_id)  # the cask shelf is not trimmed
    whites = sorted(p for p in by_id if p.startswith("w"))
    assert whites == ["w4", "w5", "w6", "w7", "w8"]  # the five cheapest; the sparkling one never counts
    assert sorted(p for p in by_id if p.startswith("r")) == ["r1", "r2", "r3"]
    assert by_id["w8"]["store_category"] == "beer-wine-and-cider/white-wine"
    assert by_id["c1"]["store_category"] == "beer-wine-and-cider/cask-wine"
    assert result["stats"]["products_saved"] == 2 + 5 + 3
    assert result["stats"]["products_found"] == 2 + 9 + 3  # everything read, including the 4 wines set aside


def test_a_long_white_wine_shelf_is_read_to_the_end_before_choosing(monkeypatch, tmp_path):
    big = {f"b{n}": {"name": f"Bottle {n}", "size": "750ml", "price": 20.0 - n * 0.1,
                     "unit_price": round((20.0 - n * 0.1) / 0.75, 2), "unit": "L"} for n in range(60)}
    WINES.update(big)
    url = WINE + "white-wine"
    pages = {url: [f"b{n}" for n in range(50)], url + "?pg=2": [f"b{n}" for n in range(50, 60)]}
    site, result = _scrape_wines(monkeypatch, tmp_path, pages, f"{url} pages=1 category=white-wine\n")
    assert site.loaded == [url, url + "?pg=2"]
    assert sorted(p["product_id"] for p in result["products"]) == [f"b{n}" for n in range(55, 60)]


def test_other_categories_are_not_trimmed(monkeypatch, tmp_path):
    pages = {BASE + "pantry/baking-supplies--sugar": [f"w{n}" for n in range(1, 9)]}
    _, result = _scrape_wines(monkeypatch, tmp_path, pages, f"{BASE}pantry/baking-supplies--sugar category=baking\n")
    assert len(result["products"]) == 8


# --- Woolworths ---------------------------------------------------------------------------

def _ww(pid, name, price, per_litre, **extra):
    return {"product_id": pid, "name": name, "category": "x", "size": "750ml", "price": price,
            "unit_price": per_litre, "unit": "L", "scraped_at": "2026-01-01", "supermarket": "Woolworths", **extra}


def test_woolworths_wine_shelves_are_trimmed_and_the_cask_shelf_is_not(monkeypatch):
    shelves = {
        "beer-wine/white-wine": [_ww(f"w{n}", f"white wine {n}", 10 + n, 13.0 + n) for n in range(8)]
        + [_ww("sp", "sparkling white", 4.0, 5.0)],
        "beer-wine/red-wine": [_ww("r1", "red wine", 9.0, 12.0, promo_type="member", regular_pack_price=14.0, unit_raw="$18.67/1L")],
        "beer-wine/cask-wine": [_ww(f"c{n}", f"cask {n}", 12.0 + n, 6.0 + n, size="2L") for n in range(7)],
    }
    monkeypatch.setattr(woolworths, "browse_products", lambda path, limit, label, stat=None: (shelves[path], [], len(shelves[path])))
    result = woolworths.browse_all([woolworths.BrowsePage(p, p.split("/")[1]) for p in shelves])
    ids = {p["product_id"] for p in result["products"]}
    assert {f"w{n}" for n in range(5)} <= ids and not ids & {f"w{n}" for n in range(5, 8)} and "sp" not in ids
    assert {f"c{n}" for n in range(7)} <= ids and "r1" in ids


# --- the shipped categories files ----------------------------------------------------------

HERBS = {"coriander", "dill", "parsley", "basil", "mint", "chive", "thyme", "rosemary"}


@pytest.mark.parametrize("name,host", [("categories.txt", "paknsave"), ("newworld_categories.txt", "newworld")])
def test_foodstuffs_files_list_the_herb_and_wine_shelves(name, host):
    pages = {p.url: p for p in parse_categories_file(os.path.join(ROOT, name)) if "?pg=" not in p.url}
    base = f"https://www.{host}.co.nz/shop/category/"
    herbs = pages[base + "fruit-and-vegetables/fresh-salad--herbs"]
    assert herbs.category == "fresh-herbs" and HERBS <= set(herbs.covers)
    assert pages[base + "beer-wine-and-cider/cask-wine"].category == "cask-wine"
    assert pages[base + "beer-wine-and-cider/white-wine"].category == "white-wine"
    assert pages[base + "beer-wine-and-cider/red-wine"].category == "red-wine"
    assert base + "meat-poultry-and-seafood/seafood" in pages


def test_woolworths_file_lists_herbs_seafood_and_wine():
    pages = {p.path: p for p in woolworths.parse_categories_file(woolworths.DEFAULT_CATEGORIES_PATH)}
    assert HERBS <= set(pages["fruit-veg/fresh-salad-herbs"].covers)
    for path in ("fish-seafood/prawns-seafood", "fish-seafood/fish", "beer-wine/cask-wine",
                 "beer-wine/white-wine", "beer-wine/red-wine"):
        assert path in pages
    assert "mussel" in pages["fish-seafood/prawns-seafood"].covers


# --- mussels in the shell ----------------------------------------------------------------------

IN_SHELL = [
    "Fresh Greenshell Mussels 1kg", "Sanford Live Greenshell Mussels Per Kg", "Live Mussels 1kg",
    "Fresh Mussels (per kg)", "Greenshell Mussels Whole Shell 1kg", "Mussels In The Shell Per Kg",
]
NOT_IN_SHELL = [
    "Mussel Meat 500g", "Sanford Marinated Mussels Garlic 500g", "Smoked Mussels in Oil 105g",
    "Greenshell Mussels Frozen Meat 454g", "Greenshell Mussels Half Shell 1kg", "Whole Cooked Greenshell Mussels Shell On",
]


@pytest.mark.parametrize("term", ["fresh greenshell mussels (per kg)", "live mussels (1kg)", "mussels in the shell", "fresh mussels"])
def test_in_shell_mussel_terms_match_the_in_shell_products_only(term):
    matched = {n for n in IN_SHELL + NOT_IN_SHELL if is_local_match(term, n)}
    assert not matched & set(NOT_IN_SHELL)
    assert matched  # at least the plainly named ones


def test_per_kg_does_not_stop_a_fresh_mussel_term_matching():
    assert is_local_match("fresh greenshell mussels (per kg)", "Fresh Greenshell Mussels 1kg")
    assert is_local_match("fresh greenshell mussels (per kg)", "Sanford Live Greenshell Mussels Per Kg")


def test_a_live_term_finds_a_fresh_product_and_the_other_way_round():
    assert is_local_match("live mussels (1kg)", "Fresh Mussels Per Kg")
    assert is_local_match("fresh mussels", "Live Mussels 1kg")


def test_the_whole_shell_name_is_not_taken_for_a_shell_item():
    products = [{"name": n} for n in IN_SHELL + NOT_IN_SHELL]
    names = {p["name"] for p in match_products("mussels in the shell", products)}
    assert "Greenshell Mussels Whole Shell 1kg" in names and "Mussel Meat 500g" not in names


def test_a_term_of_only_the_word_per_matches_nothing():
    assert not is_local_match("per", "Per Se")


# --- tiles that show no unit price ------------------------------------------------------------

def test_a_wine_with_no_unit_price_gets_one_from_its_size_and_the_stores_own_is_kept():
    bare = {"name": "Cleanskin Chardonnay", "size": "750ml", "price": 7.99, "unit_price": None, "unit": None, "unit_raw": ""}
    assert cooking_wine.fill_unit_price(bare)
    assert (bare["unit_price"], bare["unit"], bare["unit_raw"]) == (10.65, "L", "$10.65/1L")
    own = {"name": "Wine", "size": "750ml", "price": 7.99, "unit_price": 11.0, "unit": "L", "unit_raw": "$11.00/1L"}
    assert not cooking_wine.fill_unit_price(own) and own["unit_price"] == 11.0
    assert not cooking_wine.fill_unit_price({"name": "Gift", "size": "ea", "price": 9.0, "unit_price": None, "unit_raw": ""})


def test_the_published_fields_for_a_wine_are_per_litre(monkeypatch):
    from scraper.item_fields import build_item_fields

    cask = {"name": "Country Dry White Cask", "size": "3l", "price": 33.99, "unit_price": None, "unit": None,
            "unit_raw": "", "pack_price": 33.99, "regular_pack_price": 33.99}
    cooking_wine.select_for_shelf("beer-wine-and-cider/cask-wine", [cask])
    payload = build_item_fields(cask).payload
    assert payload["unit"] == "L" and payload["price"] == 11.33 and payload["pack_price"] == 33.99


# --- Woolworths mussel names read from the live shelf ---------------------------------------------

WOOLWORTHS_MUSSELS = [
    "united fish co greenshell mussels raw whole shell", "mills bay mussels greenshell",
    "talleys mussels cooked & marinated plain", "woolworths mussels marinated classic",
    "aquafresh smoked mussels natural", "talleys mussels lime & coriander", "Sanford New Zealand Greenshell Mussel Meat",
]


@pytest.mark.parametrize("term", ["fresh greenshell mussels (per kg)", "live mussels (1kg)", "mussels in the shell"])
def test_woolworths_in_shell_mussels_are_found_and_the_jars_and_meat_are_not(term):
    names = {p["name"] for p in match_products(term, [{"name": n} for n in WOOLWORTHS_MUSSELS])}
    assert names == {"united fish co greenshell mussels raw whole shell", "mills bay mussels greenshell"}
