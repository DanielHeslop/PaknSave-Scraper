"""Tests for unit_price.py's rescaling logic."""

from __future__ import annotations

from scraper.unit_price import derive_from_size_and_price, parse_raw_unit_price


def test_grams_rescaled_to_per_kg():
    result = parse_raw_unit_price("$0.49/100g")
    assert result is not None
    assert result.amount == 4.90
    assert result.unit == "kg"


def test_litre_passes_through_unchanged():
    result = parse_raw_unit_price("$1.64/1L")
    assert result is not None
    assert result.amount == 1.64
    assert result.unit == "L"


def test_each_passes_through_unchanged():
    result = parse_raw_unit_price("$2.00/each")
    assert result is not None
    assert result.amount == 2.00
    assert result.unit == "each"


def test_malformed_input_returns_none_not_a_guess():
    result = parse_raw_unit_price("this is not a unit price at all")
    assert result is None


def test_unrecognised_unit_shape_returns_none():
    # A price-per-slash pattern exists, but "widget" isn't a real unit -
    # this must not be guessed at.
    result = parse_raw_unit_price("$3.00/widget")
    assert result is None


def test_ml_rescaled_to_per_litre():
    result = parse_raw_unit_price("$0.55/300ml")
    assert result is not None
    assert abs(result.amount - 1.83) < 0.01
    assert result.unit == "L"


def test_derive_from_override_size_grams():
    result = derive_from_size_and_price("1020g", 21.99)
    assert result is not None
    assert result.unit == "kg"
    assert abs(result.amount - 21.56) < 0.01


def test_derive_from_override_size_unrecognised_returns_none():
    result = derive_from_size_and_price("6pk", 5.99)
    assert result is None


# Real "$X/Yunit" text confirmed on the live vegetables category page via
# step0_verify.py (with playwright-stealth) on 2026-09-02 - not invented
# examples. "1kg" (a number directly followed by the unit, not bare "kg")
# and "ea" (not the literal word "each") are the two live-site shapes these
# cases exist to pin down.
def test_real_example_229_per_1kg():
    result = parse_raw_unit_price("$2.29/1kg")
    assert result is not None
    assert result.amount == 2.29
    assert result.unit == "kg"


def test_real_example_599_per_1kg():
    result = parse_raw_unit_price("$5.99/1kg")
    assert result is not None
    assert result.amount == 5.99
    assert result.unit == "kg"


def test_real_example_150_per_ea():
    result = parse_raw_unit_price("$1.50/ea")
    assert result is not None
    assert result.amount == 1.50
    assert result.unit == "each"


def test_real_example_199_per_1kg():
    result = parse_raw_unit_price("$1.99/1kg")
    assert result is not None
    assert result.amount == 1.99
    assert result.unit == "kg"


def test_real_example_1099_per_1kg():
    result = parse_raw_unit_price("$10.99/1kg")
    assert result is not None
    assert result.amount == 10.99
    assert result.unit == "kg"


if __name__ == "__main__":
    tests = [
        test_grams_rescaled_to_per_kg,
        test_litre_passes_through_unchanged,
        test_each_passes_through_unchanged,
        test_malformed_input_returns_none_not_a_guess,
        test_unrecognised_unit_shape_returns_none,
        test_ml_rescaled_to_per_litre,
        test_derive_from_override_size_grams,
        test_derive_from_override_size_unrecognised_returns_none,
        test_real_example_229_per_1kg,
        test_real_example_599_per_1kg,
        test_real_example_150_per_ea,
        test_real_example_199_per_1kg,
        test_real_example_1099_per_1kg,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print("All unit_price.py tests passed.")


# --- per-100-sheets and other formats found in the audit ---------------------


def test_per_100_sheets_with_space_is_not_truncated():
    result = parse_raw_unit_price("$0.26/100 sheets")
    assert result is not None
    assert (result.amount, result.unit) == (0.26, "100sheets")


def test_per_100_sheets_pams_and_paper_towels():
    for text, amount in [("$0.13/100 sheets", 0.13), ("$1.91/100 sheets", 1.91), ("$0.47/100 sheets", 0.47)]:
        result = parse_raw_unit_price(text)
        assert (result.amount, result.unit) == (amount, "100sheets")


def test_100ss_maps_to_100sheets():
    result = parse_raw_unit_price("$0.39/100ss")
    assert (result.amount, result.unit) == (0.39, "100sheets")


def test_sheet_counts_other_than_100_are_rescaled():
    result = parse_raw_unit_price("$0.50/50 sheets")
    assert (result.amount, result.unit) == (1.0, "100sheets")
    result = parse_raw_unit_price("$1.00/200ss")
    assert (result.amount, result.unit) == (0.5, "100sheets")


def test_100ea_is_rescaled_to_one_each():
    result = parse_raw_unit_price("$4.75/100ea")
    assert (result.amount, result.unit) == (0.0475, "each")


def test_per_each_variants():
    for text in ["$0.12/ea", "$0.71/ea", "$0.12/1ea", "$0.12 per each", "$0.12/each"]:
        result = parse_raw_unit_price(text)
        assert result is not None, text
        assert result.unit == "each"
        assert result.amount == 0.12 or text.startswith("$0.71")


def test_per_word_form_and_spaces():
    result = parse_raw_unit_price("$1.64 per 1L")
    assert (result.amount, result.unit) == (1.64, "L")
    result = parse_raw_unit_price("$0.49 / 100 g")
    assert (result.amount, result.unit) == (4.9, "kg")


def test_millilitre_measure_still_parses():
    result = parse_raw_unit_price("$0.40/10mL")
    assert (result.amount, result.unit) == (40.0, "L")


def test_bare_100_is_sheets_only_for_facial_tissue():
    for name in ["Paseo Soft & Strong Tissues 200pk", "Kleenex Large & Thick Facial Tissue", "Sorbent Silky White Tissues"]:
        result = parse_raw_unit_price("$0.95/100", name)
        assert result is not None, name
        assert (result.amount, result.unit) == (0.95, "100sheets")


def test_bare_100_for_other_products_stays_unparsed():
    assert parse_raw_unit_price("$37.18/100", "Surf Laundry Capsules 50pk") is None
    assert parse_raw_unit_price("$0.95/100", "Pams Toilet Tissue 12pk") is None
    assert parse_raw_unit_price("$0.95/100", "Handee Paper Towels") is None
    assert parse_raw_unit_price("$0.95/100", "Huggies Baby Wipes") is None
    assert parse_raw_unit_price("$0.95/100") is None


def test_formats_that_fit_no_unit_stay_unparsed():
    for text in ["$0.80/wash", "$1.20/metre", "$2.10/m", "$0.40/10", "", "no price here"]:
        assert parse_raw_unit_price(text) is None, text


def test_wipes_counted_in_sheets_are_priced_per_wipe():
    result = parse_raw_unit_price("$8.84/100ss", "silk personal wipes flushable")
    assert (result.amount, result.unit) == (0.0884, "each")


def test_toilet_paper_in_sheets_is_not_treated_as_wipes():
    result = parse_raw_unit_price("$0.39/100ss", "cotton softs toilet paper 3ply mega")
    assert result.unit == "100sheets"
