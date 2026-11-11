"""Tests for scraper/pack_size.py: pack counts and sheet counts from explicit text only."""

from __future__ import annotations

from scraper.pack_size import parse_pack, sheets_from_text


def test_subtitle_multi_sheets():
    info = parse_pack("Paseo Pure Luxury 3ply", "8 x 270 sheets")
    assert (info.count, info.sheets_per_item) == (8, 270)
    assert info.count_source == "size"


def test_subtitle_two_by_sixty_sheets():
    info = parse_pack("Tuffy Paper Towels", "2 x 60 sheets")
    assert (info.count, info.sheets_per_item) == (2, 60)


def test_subtitle_12pk():
    info = parse_pack("Pams Free Range Eggs", "12pk")
    assert info.count == 12
    assert info.sheets_per_item is None


def test_woolworths_volume_size_40pack():
    info = parse_pack("Purex Mega Rolls", "40pack")
    assert info.count == 40


def test_woolworths_package_type_4_pack():
    info = parse_pack("Quilton Toilet Tissue", None, "4 pack")
    assert info.count == 4
    assert info.count_source == "package_type"


def test_name_pk():
    assert parse_pack("Cotton Softs Toilet Paper 4pk").count == 4
    assert parse_pack("Cotton Softs Toilet Paper 40pk").count == 40


def test_name_multi_weight():
    info = parse_pack("Yoghurt Pottles 6 x 125g")
    assert info.count == 6
    assert info.sheets_per_item is None


def test_name_items_word():
    assert parse_pack("Flushable Wipes 40 wipes").count == 40


def test_dimension_is_not_a_pack():
    info = parse_pack("Baking Paper 30 x 20cm")
    assert info.count is None


def test_nothing_found_returns_none():
    info = parse_pack("Anchor Blue Milk Powder", "1kg")
    assert info.count is None
    assert info.sheets_per_item is None


def test_conflicting_counts_are_flagged_and_size_wins():
    info = parse_pack("Eggs 6pk", "12pk")
    assert info.count == 12
    assert info.count_conflict is True


def test_agreeing_counts_are_not_a_conflict():
    info = parse_pack("Eggs 12pk", "12pk")
    assert info.count == 12
    assert info.count_conflict is False


def test_sheets_per_roll_phrase():
    assert sheets_from_text("Soft and strong. 180 sheets per roll.") == 180


def test_single_bare_sheet_number():
    assert sheets_from_text("<p>Each pack has 180 sheets</p>") == 180


def test_two_different_sheet_numbers_are_ambiguous():
    assert sheets_from_text("CottonSofts Posh 3ply Mega Roll 400 sheets is more than double our standard Posh 170 sheet roll.") is None


def test_multi_sheet_text_is_not_used_for_bare_sheets():
    assert sheets_from_text("8 x 270 sheets") is None


def test_no_sheets_text():
    assert sheets_from_text("Great for the whole family") is None
    assert sheets_from_text(None) is None


def test_description_used_for_sheets_when_name_has_none():
    info = parse_pack("Purex Mega", "40pack", None, "180 sheets per roll")
    assert (info.count, info.sheets_per_item) == (40, 180)
    assert info.sheets_source == "description"


def test_ea_size_is_a_count():
    assert parse_pack("Yarrows Mock Creme Filled Donuts", "4ea").count == 4


def test_egg_size_and_grade_are_not_counts():
    info = parse_pack("Woodland Free Range Size 6 Eggs", "18pk")
    assert info.count == 18
    assert info.count_conflict is False
    assert parse_pack("Woodland Free Range Grade 7 Eggs", "6pk").count_conflict is False


def test_s_count_in_size():
    assert parse_pack("Persil Ultimate Laundry Capsules", "17s").count == 17
    assert parse_pack("Vitamin C", "90s").count == 90
    assert parse_pack("Vitamin C", "30's").count == 30


def test_s_count_ignored_in_names():
    assert parse_pack("Kleenex Ultra Soft Tissues Pocket Pack 60's").count is None
