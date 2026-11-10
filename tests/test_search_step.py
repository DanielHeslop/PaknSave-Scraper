import time

from scraper.browser import parse_categories_file
from scraper.ingredients import StoreLists
from scraper.matching import is_confident_match, is_local_match
from scraper.search_step import (
    category_words,
    dedupe_products,
    local_search,
    plan_terms,
    summary_line,
    SearchOutcome,
)


def _p(pid, name, supermarket="Pak'nSave"):
    return {"product_id": pid, "name": name, "supermarket": supermarket}


def test_plurals_match_locally_but_not_in_the_strict_matcher():
    assert not is_confident_match("courgette", "Green Courgettes")
    assert is_local_match("courgette", "Green Courgettes")
    assert is_local_match("leeks", "Leek")
    assert is_local_match("tomatoes", "Truss Tomatoes")
    assert not is_local_match("saffron", "Green Courgettes")


def test_dedupe_by_supermarket_and_product_id_first_wins():
    products = [_p("P1", "a"), _p("P1", "a again"), _p("P1", "other chain", "New World"), _p("P2", "b")]
    assert [p["name"] for p in dedupe_products(products)] == ["a", "other chain", "b"]


def test_plan_terms_order_skip_and_watch_terms_never_skipped():
    lists = StoreLists(
        store="paknsave",
        search_terms=["Egg", "garlic", "honey", "egg"],
        watch_terms=["Garlic", "soap", "honey"],
        skip=frozenset({"egg", "garlic", "honey"}),
    )
    terms, skipped = plan_terms(lists)
    # honey and garlic are watched, so a skip entry doesn't stop them; Egg is only skipped.
    assert terms == ["garlic", "honey", "soap"]
    assert skipped == ["Egg"]


def test_not_found_only_when_a_scraped_category_could_have_held_it():
    products = [_p("P1", "Pams Olive Oil"), _p("P2", "Green Courgettes")]
    scraped = category_words("oils-vinegar", ("oil", "olive")) | category_words("vegetables", ("courgette", "leek"))
    outcome = local_search(["olive oil", "courgette", "leek", "saffron", "toothpaste"], products, scraped)
    assert set(outcome.found) == {"olive oil", "courgette"}
    assert outcome.not_found == ["leek"]  # vegetables was scraped, nothing matched
    assert set(outcome.unreported) == {"saffron", "toothpaste"}  # no scraped category for them


def test_category_not_scraped_means_no_not_found_claim():
    outcome = local_search(["toothpaste"], [_p("P1", "Pams Olive Oil")], category_words("oils-vinegar", ("oil",)))
    assert outcome.not_found == [] and outcome.unreported == ["toothpaste"]


def test_deadline_stops_the_search_and_lists_what_was_left():
    outcome = local_search(["a1", "b2"], [], set(), deadline=time.monotonic() - 1)
    assert outcome.searched == [] and outcome.unsearched == ["a1", "b2"]


def test_matches_across_terms_publish_once():
    products = [_p("P1", "Garlic Bulb")]
    outcome = local_search(["garlic", "bulb"], products, set())
    assert len(outcome.matched_products) == 1


def test_summary_line_fields():
    outcome = SearchOutcome(searched=["a", "b", "c"], found={"a": []}, not_found=["b"], skipped=["d"])
    line = summary_line(12, outcome, 5, 125)
    assert line == (
        "SUMMARY: products published 12, terms searched 3, found 1, not found 1, skipped 1, "
        "categories scraped 5, run time 2m05s"
    )


def test_categories_file_covers_parsing(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text(
        "https://x/shop/category/a/b pages=2 category=lab covers=Soap,Wash\n"
        "https://x/shop/category/c\n"
    )
    pages = parse_categories_file(f)
    assert [(p.category, p.covers) for p in pages] == [("lab", ("soap", "wash")), ("lab", ("soap", "wash")), ("c", ())]
    assert pages[1].url.endswith("?pg=2")


def test_shipped_category_files_parse_and_avoid_disallowed_search_paths():
    import os
    from scraper.robots import RobotsRules

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rules = RobotsRules.parse("User-agent: *\nDisallow: /shop/search\nDisallow: /*?store=\nDisallow: /search\n")
    for name in ("categories.txt", "newworld_categories.txt"):
        pages = parse_categories_file(os.path.join(root, name))
        assert len(pages) > 40
        assert all(rules.is_allowed(p.url) for p in pages)
        labels = {p.category for p in pages}
        assert {"bulk-foods", "kitchen-cleaners", "bathroom-toilet-cleaners", "toothpaste", "laundry"} <= labels
