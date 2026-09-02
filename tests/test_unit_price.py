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
