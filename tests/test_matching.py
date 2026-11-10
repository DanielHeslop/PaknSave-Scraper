import pytest

from scraper.matching import is_local_match, match_products
from scraper.search_step import local_search


def _p(pid, name):
    return {"product_id": pid, "name": name, "supermarket": "New World"}


def _found(term, *names):
    products = [_p(f"P{i}", n) for i, n in enumerate(names)]
    return [p["name"] for p in match_products(term, products)]


def test_dry_white_wine_is_not_found_via_dry_or_white():
    names = ["Dry Roasted Peanuts", "White Bread", "Dry Dog Food", "White Chocolate Block", "Whitebait"]
    assert _found("dry white wine", *names) == []
    assert _found("chardonnay dry white wine", *names) == []


def test_dry_white_wine_found_when_a_white_wine_exists():
    assert _found("dry white wine", "Villa Maria Sauvignon Blanc White Wine", "White Bread") == [
        "Villa Maria Sauvignon Blanc White Wine"
    ]


@pytest.mark.parametrize("term", ["dry", "white", "light", "fresh", "dark"])
def test_single_generic_words_never_match(term):
    assert _found(term, "Light Bulb", "Fresh Milk", "Dark Chocolate", "White Rice", "Dry Biscuits") == []


def test_active_sourdough_starter_is_not_found():
    assert _found("active sourdough starter", "Sourdough Bread Loaf", "Active Wear Socks", "Bread Starter Kit") == []


def test_garlic_matches_garlic_but_not_a_garlic_and_herb_product():
    names = ["Garlic", "Garlic & Herb Butterfly Chicken", "Chicken Garlic and Herb Marinated Thighs", "Garlic Bulb 3pk"]
    assert _found("garlic", *names) == ["Garlic", "Garlic Bulb 3pk"]


def test_honey_rosemary_flavour_pair_is_not_honey():
    assert _found("honey", "Pams Honey & Rosemary Lamb Leg", "Manuka Honey") == ["Manuka Honey"]


def test_flavour_pair_at_the_end_is_part_of_the_product():
    assert _found("cream cheese", "Philadelphia Cream Cheese & Chives") == ["Philadelphia Cream Cheese & Chives"]


def test_term_that_itself_has_the_pair_matches_it():
    assert _found("garlic and herb butter", "Garlic & Herb Butter Block") == ["Garlic & Herb Butter Block"]


def test_milk_does_not_match_milk_powder():
    assert _found("milk", "Anchor Blue Top Milk 2L", "Anchor Milk Powder 1kg", "Milk Chocolate Drink") == [
        "Anchor Blue Top Milk 2L"
    ]


def test_milk_powder_matches_milk_powder():
    assert _found("milk powder", "Anchor Milk Powder 1kg", "Anchor Blue Top Milk 2L") == ["Anchor Milk Powder 1kg"]


def test_light_soy_sauce_matches_soy_sauce_by_dropping_the_descriptor():
    assert _found("light soy sauce", "Kikkoman Soy Sauce 250ml", "Soy Milk") == ["Kikkoman Soy Sauce 250ml"]


def test_descriptor_is_kept_when_something_matches_with_it():
    assert _found("dark soy sauce", "Lee Kum Kee Dark Soy Sauce", "Kikkoman Soy Sauce") == ["Lee Kum Kee Dark Soy Sauce"]


def test_every_main_word_is_required():
    assert _found("streaky bacon", "Streaky Bacon 250g", "Middle Bacon", "Streaky Pork") == ["Streaky Bacon 250g"]
    assert _found("tomato paste", "Tomato Paste", "Tomato Sauce", "Tomatoes", "Curry Paste") == ["Tomato Paste"]


@pytest.mark.parametrize(
    "product",
    [
        "Chicken Stock Powder",
        "Chicken Stock Mix",
        "Chicken Stock Kit",
        "Chicken Stock Sauce",
        "Chicken Stock Seasoning",
        "Chicken Stock Spread",
        "Chicken Flavoured Stock Cubes",
        "Chicken Stock Drink",
    ],
)
def test_other_forms_are_rejected_unless_the_term_has_that_form(product):
    assert _found("chicken stock", product) == []


@pytest.mark.parametrize("product", ["Garlic Mayonnaise", "Garlic Granola", "Garlic Muffins", "Garlic Cookies", "Garlic Chips"])
def test_more_rejected_forms(product):
    assert _found("garlic", product) == []


def test_form_word_in_the_term_allows_that_form():
    assert _found("chilli powder", "Hot Chilli Powder 50g", "Chilli Sauce") == ["Hot Chilli Powder 50g"]
    assert _found("mayonnaise", "Best Foods Mayonnaise") == ["Best Foods Mayonnaise"]
    assert _found("corn chip", "Doritos Corn Chips") == ["Doritos Corn Chips"]


def test_synonyms_and_plurals():
    assert _found("yoghurt", "Greek Yogurt Natural") == ["Greek Yogurt Natural"]
    assert _found("yogurt", "Fresh'n Fruity Yoghurt") == ["Fresh'n Fruity Yoghurt"]
    assert _found("chilli", "Red Chili 100g") == ["Red Chili 100g"]
    assert _found("chili", "Green Chilli") == ["Green Chilli"]
    assert _found("courgette", "Green Courgettes", "Zucchini 1kg") == ["Green Courgettes", "Zucchini 1kg"]
    assert _found("tomatoes", "Truss Tomato") == ["Truss Tomato"]
    assert _found("molasse", "Molasses 500g") == ["Molasses 500g"]


def test_descriptors_dropped_only_once_and_only_if_nothing_matches():
    assert _found("fresh frozen organic peas", "Frozen Peas", "Organic Peas", "Green Beans") == ["Frozen Peas", "Organic Peas"]
    assert _found("frozen peas", "Garden Peas") == ["Garden Peas"]
    assert _found("frozen peas", "Frozen Peas", "Garden Peas") == ["Frozen Peas"]


def test_term_of_only_descriptors_never_matches_even_after_dropping():
    assert _found("dry fresh", "Dry Fresh Pasta") == []


def test_is_local_match_single_product_form():
    assert is_local_match("courgette", "Green Courgettes")
    assert not is_local_match("light soy sauce", "Kikkoman Soy Sauce")
    assert is_local_match("light soy sauce", "Kikkoman Soy Sauce", drop_descriptors=True)


def test_local_search_uses_the_strict_rules():
    products = [_p("1", "Garlic"), _p("2", "Garlic & Herb Butterfly Chicken"), _p("3", "Milk Powder"), _p("4", "Blue Top Milk")]
    outcome = local_search(["garlic", "milk", "dry white wine"], products, {"garlic", "milk", "wine"})
    assert [p["name"] for p in outcome.found["garlic"]] == ["Garlic"]
    assert [p["name"] for p in outcome.found["milk"]] == ["Blue Top Milk"]
    assert outcome.not_found == ["dry white wine"]


def test_honey_and_lemon_do_not_match_yoghurts():
    assert _found("honey", "Honey Yoghurt", "Greek Honey Yoghurt 1kg", "Manuka Honey 500g") == ["Manuka Honey 500g"]
    assert _found("lemon", "Lemon Yoghurt", "Fresh 'n Fruity Zesty Lemon Yoghurt", "Lemons") == ["Lemons"]


def test_butter_does_not_match_butter_chicken():
    assert _found("butter", "Butter Chicken", "Wattie's Butter Chicken 350g", "Garlic Butter Chicken Kievs") == []


def test_garlic_does_not_match_beef_and_black_garlic_sausages():
    assert _found("garlic", "Beef & Black Garlic Sausages", "Harmony Beef & Black Garlic Sausages 400g") == []


def test_tamari_matches_tamari_soy_sauce_as_a_kind_of_soy_sauce():
    assert _found("tamari", "Organic Tamari Soy Sauce", "Kikkoman Soy Sauce") == ["Organic Tamari Soy Sauce"]


def test_tamari_exception_does_not_let_other_nouns_through():
    assert _found("tamari", "Tamari Almonds", "Soy Tamari Rice Crackers") == []


def test_garlic_still_matches_bulbs_and_pack_sizes():
    names = ["Garlic Bulb 3pk", "Garlic (5pk)", "Garlic", "Pams Crushed Garlic", "Masterfoods Finely Crushed Garlic Jar"]
    assert _found("garlic", *names) == names


def test_butter_still_matches_salted_and_unsalted_butter():
    names = ["Anchor Salted Butter 500g", "Mainland Unsalted Butter 250g", "Pams Butter Block 500g"]
    assert _found("butter", *names) == names


def test_honey_still_matches_honey_jars():
    names = ["Manuka Honey Jar 500g", "Airborne Honey 500g", "Pams Squeeze Honey Bottle", "Clover Honey (1kg)"]
    assert _found("honey", *names) == names


def test_text_after_with_in_and_infused_is_not_the_main_item():
    assert _found("sardines", "Pams Sardines In Olive Oil") == ["Pams Sardines In Olive Oil"]
    assert _found("olive oil", "Pams Sardines In Olive Oil") == []
    assert _found("tomato", "Diced Tomatoes With Garlic And Oregano") == ["Diced Tomatoes With Garlic And Oregano"]
    assert _found("garlic", "Diced Tomatoes With Garlic And Oregano") == []
    assert _found("olive oil", "Olive Oil Infused With Chilli") == ["Olive Oil Infused With Chilli"]
    assert _found("chilli", "Olive Oil Infused With Chilli") == []


def test_term_with_and_keeps_its_own_pair():
    assert _found("salt and pepper", "Salt & Pepper Grinder Set") == []
    assert _found("garlic and herb butter", "Garlic & Herb Butter Block", "Garlic Butter Chicken") == ["Garlic & Herb Butter Block"]


@pytest.mark.parametrize(
    "term, product, expected",
    [
        ("honey", "Honey Yoghurt", False),
        ("lemon", "Lemon Yoghurt", False),
        ("butter", "Butter Chicken", False),
        ("garlic", "Beef & Black Garlic Sausages", False),
        ("tamari", "Organic Tamari Soy Sauce", True),
        ("yoghurt", "Cyclops Thick & Creamy Natural Yoghurt", True),
        ("yoghurt", "Yoplait With Real Blueberry Yoghurt", True),
        ("weet-bix", "Sanitarium Weet-Bix Breakfast Cereal", True),
        ("focaccia", "Kapiti Focaccia Bread", True),
        ("butter", "Butter Chicken", False),
        ("garlic", "Garlic & Herb Butterfly Chicken", False),
    ],
)
def test_main_item_table(term, product, expected):
    assert bool(_found(term, product)) is expected


def test_with_and_in_cut_only_after_a_food_noun():
    breast = "Chicken Breast with Garlic Butter"
    assert _found("chicken breast", breast) == [breast]
    assert _found("garlic", breast) == []
    assert _found("butter", breast) == []
    assert _found("sardines", "Sardines in Olive Oil") == ["Sardines in Olive Oil"]
    assert _found("tomatoes", "Wattie's Tomatoes Chopped In Puree") == ["Wattie's Tomatoes Chopped In Puree"]


def test_with_after_a_lone_brand_is_not_a_cut():
    assert _found("strawberry", "Yoplait With Real Strawberry Yoghurt") == []
    assert _found("yoghurt", "Yoplait Greek Style With Honey Yoghurt") == ["Yoplait Greek Style With Honey Yoghurt"]
    assert _found("honey", "Yoplait Greek Style With Honey Yoghurt") == []


def test_and_and_ampersand_never_cut_the_name():
    assert _found("yoghurt", "Thick and Creamy Yoghurt", "Thick & Creamy Yoghurt") == ["Thick and Creamy Yoghurt", "Thick & Creamy Yoghurt"]
    assert _found("thick", "Thick & Creamy Yoghurt") == []
    assert _found("cheese", "Cream Cheese & Chives") == ["Cream Cheese & Chives"]
    assert _found("chives", "Cream Cheese & Chives") == ["Cream Cheese & Chives"]


@pytest.mark.parametrize(
    "term, product",
    [
        ("tamari", "Organic Tamari Soy Sauce"),
        ("weet-bix", "Sanitarium Weet-Bix Breakfast Cereal"),
        ("focaccia", "Kapiti Focaccia Bread"),
        ("pita", "Mission Pita Loaf"),
        ("ciabatta", "Ciabatta Rolls"),
        ("hummus", "Hummus Dip Spread"),
    ],
)
def test_generic_main_item_matches_the_term_before_it(term, product):
    assert _found(term, product) == [product]


def test_generic_rule_still_respects_forms_and_other_words():
    assert _found("tomato", "Pams Tomato Sauce") == []
    assert _found("tamari", "Tamari Almonds", "Soy Tamari Rice Crackers") == []
    assert _found("focaccia", "Focaccia Crisps") == []
    assert _found("weet-bix", "Sanitarium Cereal Bars") == []


def test_mix_is_generic_only_when_the_term_says_mix():
    assert _found("pancake", "Edmonds Pancake Mix") == []
    assert _found("pancake mix", "Edmonds Pancake Mix") == ["Edmonds Pancake Mix"]
