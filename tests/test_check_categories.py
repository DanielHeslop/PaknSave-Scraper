"""Checking a categories file against the cached tree: in the tree, fixed (one clear replacement), listed."""

from __future__ import annotations

from scraper.category_mapping import (
    apply_replacements,
    check_categories_file,
    suggest_replacement,
    woolworths_file_paths,
)
from scraper.category_tree import Category

BASE = "https://www.paknsave.co.nz/shop/category/"

TREE = [
    Category("pantry", "Pantry", ""),
    Category("pantry/spices-seasoning--coatings", "Spices, Seasoning & Coatings", "pantry"),
    Category("pantry/spices-seasoning--coatings/dried-herbs", "Dried Herbs", "pantry/spices-seasoning--coatings"),
    Category("pantry/baking-supplies--sugar", "Baking Supplies & Sugar", "pantry"),
    Category("pantry/bulk-foods", "Bulk Foods", "pantry"),
    Category("pantry/canned-foods--packets", "Canned Foods & Packets", "pantry"),
    Category("pantry/desserts", "Desserts", "pantry"),
    Category("pantry/pasta-rice--noodles", "Pasta, Rice & Noodles", "pantry"),
    Category("pantry/pasta-rice--noodles-bulk", "Pasta, Rice & Noodles Bulk", "pantry"),
    Category("fruit-and-vegetables", "Fruit & Vegetables", ""),
    Category("fruit-and-vegetables/fruit", "Fruit", "fruit-and-vegetables"),
]
PATHS = {c.path for c in TREE}


def write(tmp_path, *lines):
    path = tmp_path / "categories.txt"
    path.write_text("# comment\n\n" + "\n".join(lines) + "\n")
    return path


def test_the_spices_address_is_the_one_clear_replacement():
    assert suggest_replacement("pantry/spices--seasonings", PATHS) == "pantry/spices-seasoning--coatings"


def test_no_replacement_when_nothing_is_close():
    assert suggest_replacement("pantry/frozen-things", PATHS) is None
    assert suggest_replacement("pantry/zzz", PATHS) is None


def test_no_replacement_when_two_candidates_are_equally_close():
    paths = PATHS | {"pantry/pasta-rice--noodle"}
    assert suggest_replacement("pantry/pasta-rice--noodles-x", paths) is None


def test_no_replacement_across_top_levels_or_for_a_top_level_path():
    assert suggest_replacement("fruit-and-vegetables/spices-seasoning--coatings", PATHS) is None
    assert suggest_replacement("pantry-old", PATHS) is None
    assert suggest_replacement("nowhere/spices-seasoning--coatings", PATHS) is None  # parent not in the tree


def test_file_check_sorts_paths_into_in_tree_fixed_and_unresolved(tmp_path):
    path = write(
        tmp_path,
        BASE + "pantry/bulk-foods pages=2 category=bulk",
        BASE + "pantry/bulk-foods?pg=2",
        BASE + "pantry/spices--seasonings pages=3 category=spices",
        BASE + "pantry/mystery-aisle category=mystery",
        BASE + "fruit-and-vegetables/fruit",
    )
    check = check_categories_file(path, TREE)
    assert check.in_tree == ["pantry/bulk-foods", "fruit-and-vegetables/fruit"]
    assert check.fixed == [("pantry/spices--seasonings", "pantry/spices-seasoning--coatings")]
    assert check.unresolved == ["pantry/mystery-aisle"]


def test_applying_a_fix_rewrites_only_that_page_and_keeps_the_options(tmp_path):
    spices = BASE + "pantry/spices--seasonings pages=3 category=spices covers=spice,salt"
    path = write(tmp_path, BASE + "pantry/bulk-foods category=bulk", spices, "# " + BASE + "pantry/spices--seasonings")
    changed = apply_replacements(path, {"pantry/spices--seasonings": "pantry/spices-seasoning--coatings"})
    lines = path.read_text().splitlines()
    assert changed == 1
    assert lines[2] == BASE + "pantry/bulk-foods category=bulk"
    assert lines[3] == BASE + "pantry/spices-seasoning--coatings pages=3 category=spices covers=spice,salt"
    assert lines[4] == "# " + BASE + "pantry/spices--seasonings"  # comments are left alone
    assert check_categories_file(path, TREE).fixed == []


def test_woolworths_file_paths_and_check(tmp_path):
    path = tmp_path / "woolworths_categories.txt"
    path.write_text("# c\npantry/baking category=baking\npantry/tinned-foods-packets category=x covers=a\n\nhousehold/nope\n")
    assert woolworths_file_paths(path) == ["pantry/baking", "pantry/tinned-foods-packets", "household/nope"]
    tree = [Category("pantry", "Pantry", ""), Category("pantry/baking", "Baking", "pantry"), Category("household", "Household", "")]
    check = check_categories_file(path, tree, "woolworths")
    assert check.in_tree == ["pantry/baking"]
    assert check.unresolved == ["pantry/tinned-foods-packets", "household/nope"]  # a shelf level that is gone is not guessed



def test_the_command_reports_and_fixes(tmp_path, monkeypatch, capsys):
    from scraper import category_tree, check_categories

    file = write(tmp_path, BASE + "pantry/spices--seasonings pages=3", BASE + "pantry/mystery-aisle")
    monkeypatch.setattr(check_categories, "files_by_store", lambda: {"paknsave": file.name})
    monkeypatch.setattr(category_tree, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(category_tree, "load_cache", lambda store: {"categories": TREE})

    check_categories.main([])
    out = capsys.readouterr().out
    assert "fix: pantry/spices--seasonings -> pantry/spices-seasoning--coatings" in out
    assert "not in the tree, left alone: pantry/mystery-aisle" in out
    assert "spices--seasonings" in file.read_text()  # report only

    check_categories.main(["--fix"])
    text = file.read_text()
    assert "pantry/spices-seasoning--coatings pages=3" in text and "pantry/mystery-aisle" in text
