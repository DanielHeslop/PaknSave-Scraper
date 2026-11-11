"""Tests for scraper/item_fields.py: price, unit, pack and promo fields per product."""

from __future__ import annotations

import pytest

from scraper.item_fields import build_item_fields


def _fields(**p):
    p.setdefault("size", None)
    return build_item_fields(p)


def test_woolworths_40pk_toilet_paper():
    r = _fields(
        name="purex mega rolls toilet paper", size="40pack", price=10.39, pack_price=10.39,
        regular_pack_price=10.39, unit_raw="$0.14/100ss", sheets_per_item=180,
    )
    p = r.payload
    assert p["price"] == pytest.approx(0.1443, abs=0.0005)
    assert p["unit"] == "100sheets"
    assert p["pack_price"] == 10.39
    assert p["pack_count"] == 40
    assert p["sheets_per_item"] == 180
    assert p["total_sheets"] == 7200
    assert p["regular_price"] == p["price"]
    assert p["regular_pack_price"] == 10.39
    assert "promo_type" not in p
    assert "unit_unparsed" not in p


def test_woolworths_40pk_without_sheet_count_uses_store_figure():
    p = _fields(
        name="purex mega rolls toilet paper", size="40pack", price=10.39,
        unit_raw="$0.14/100ss",
    ).payload
    assert (p["price"], p["unit"], p["pack_count"]) == (0.14, "100sheets", 40)
    assert "sheets_per_item" not in p and "total_sheets" not in p


def test_woolworths_4pk_no_sheets():
    p = _fields(
        name="cotton softs toilet paper", size="4pk", price=1.89, pack_price=1.89,
        unit_raw="$0.26/100ss", package_type="4 pack",
    ).payload
    assert (p["price"], p["unit"], p["pack_price"], p["pack_count"]) == (0.26, "100sheets", 1.89, 4)
    assert "total_sheets" not in p


def test_paknsave_pams_12_x_180():
    p = _fields(
        name="Pams Value 2ply Toilet Tissue", size="12 x 180 sheets", price=2.89,
        unit_raw="$0.13/100 sheets",
    ).payload
    assert p["price"] == pytest.approx(0.1338, abs=0.0005)
    assert p["unit"] == "100sheets"
    assert p["pack_count"] == 12
    assert p["total_sheets"] == 2160
    assert p["sheets_per_item"] == 180


def test_paknsave_paseo_sale_8_x_270():
    p = _fields(
        name="Paseo Pure Luxury 3ply Toilet Tissue", size="8 x 270 sheets", price=5.69,
        pack_price=4.99, regular_pack_price=5.69, promo_type="sale", promo_label="Special",
        unit_raw="$0.26/100 sheets", promo_unit_raw="$0.23/100 sheets",
    ).payload
    assert p["price"] == pytest.approx(0.231, abs=0.001)
    assert p["regular_price"] == pytest.approx(0.2634, abs=0.001)
    assert p["pack_price"] == 4.99
    assert p["regular_pack_price"] == 5.69
    assert p["promo_type"] == "sale"
    assert p["promo_label"] == "Special"
    assert p["pack_count"] == 8
    assert p["unit"] == "100sheets"


def test_flushable_wipes_each():
    p = _fields(
        name="Pams Flushable Wipes", size="40 wipes", price=4.79, unit_raw="$0.12/ea",
    ).payload
    assert (p["price"], p["unit"], p["pack_price"], p["pack_count"]) == (0.12, "each", 4.79, 40)
    assert p["regular_price"] == 0.12


def test_egg_12pk_stays_each():
    r = _fields(name="Free Range Eggs", size="12pk", price=8.5, unit_raw="$0.71/ea")
    p = r.payload
    assert (p["price"], p["unit"], p["pack_count"], p["pack_price"]) == (0.71, "each", 12, 8.5)
    assert not [n for n in r.notes if n.startswith("each_mismatch")]


def test_each_mismatch_is_reported_not_hidden():
    r = _fields(name="Free Range Eggs", size="12pk", price=12.0, unit_raw="$0.71/ea")
    assert any(n.startswith("each_mismatch") for n in r.notes)
    assert r.payload["pack_count"] == 12


def test_single_each_item():
    p = _fields(name="Cucumber", size="ea", price=1.5).payload
    assert (p["price"], p["unit"], p["pack_price"], p["pack_count"]) == (1.5, "each", 1.5, 1)
    assert "unit_unparsed" not in p


def test_pack_with_no_store_unit_price_divides_by_count():
    p = _fields(name="Yoghurt Pottles 6 x 125g", size="6 x 125g", price=6.0).payload
    assert (p["price"], p["unit"], p["pack_count"], p["pack_price"]) == (1.0, "each", 6, 6.0)


def test_unknown_count_posts_single_each_and_reports_unparsed():
    p = _fields(name="Mystery Wash Tabs", size=None, price=9.99, unit_raw="$0.40/wash").payload
    assert (p["price"], p["unit"], p["pack_price"], p["pack_count"]) == (9.99, "each", 9.99, 1)
    assert p["unit_unparsed"] == "$0.40/wash"


def test_each_price_far_below_pack_price_with_no_count_is_flagged():
    r = _fields(name="Mystery Wipes", size=None, price=4.79, unit_raw="$0.12/ea")
    p = r.payload
    assert (p["price"], p["pack_count"]) == (4.79, 1)
    assert p["unit_unparsed"] == "$0.12/ea"
    assert "each_count_unknown" in r.notes


def test_kg_item_unchanged():
    p = _fields(name="Brown Onions", size="kg", price=3.49, unit_raw="$3.49/1kg").payload
    assert (p["price"], p["unit"]) == (3.49, "kg")
    assert p["regular_price"] == 3.49 and p["pack_price"] == 3.49


def test_litre_item_unchanged():
    p = _fields(name="Milk", size="2L", price=3.3, unit_raw="$1.65/1L").payload
    assert (p["price"], p["unit"]) == (1.65, "L")


def test_kg_sale_uses_promo_unit_price():
    p = _fields(
        name="Beef Mince 500g", size="500g", price=12.0, pack_price=9.0, regular_pack_price=12.0,
        promo_type="sale", promo_label="Special", unit_raw="$24.00/1kg", promo_unit_raw="$18.00/1kg",
    ).payload
    assert (p["price"], p["regular_price"], p["unit"]) == (18.0, 24.0, "kg")
    assert (p["pack_price"], p["regular_pack_price"]) == (9.0, 12.0)


def test_member_price_clubcard():
    p = _fields(
        name="Paseo Tissues 200pk", size="200pk", price=2.5, pack_price=2.09, regular_pack_price=2.5,
        promo_type="member", promo_label="Club Plus Deal", unit_raw="$1.25/100", promo_unit_raw="$1.05/100",
    ).payload
    assert p["promo_type"] == "member"
    assert p["promo_label"] == "Club Plus Deal"
    assert p["price"] < p["regular_price"]
    assert p["unit"] == "100sheets"
    assert p["sheets_per_item"] == 200 and p["total_sheets"] == 200
    assert p["pack_count"] == 1


def test_woolworths_special_sale_price_below_original():
    p = _fields(
        name="quilton toilet tissue", size="8pk", price=9.99, pack_price=9.99, regular_pack_price=13.5,
        promo_type="sale", promo_label="Special", unit_raw="$0.30/100ss", promo_unit_raw="$0.22/100ss",
    ).payload
    assert p["pack_price"] == 9.99 and p["regular_pack_price"] == 13.5
    assert p["price"] == 0.22 and p["regular_price"] == 0.30
    assert p["promo_type"] == "sale"


def test_sale_without_promo_unit_text_scales_by_price_ratio():
    p = _fields(
        name="Pams Eggs 12pk", size="12pk", price=6.0, pack_price=4.5, regular_pack_price=6.0,
        promo_type="sale", unit_raw="$0.50/ea",
    ).payload
    assert p["regular_price"] == 0.5
    assert p["price"] == pytest.approx(0.375)


def test_multibuy_keeps_regular_price():
    p = _fields(
        name="Anchor Protein Yoghurt 180g", size="180g", price=3.79, pack_price=3.79,
        regular_pack_price=3.79, promo_type="multibuy", promo_label="2 for $5.00", promo_min_qty=2,
        unit_raw="$21.05/1kg",
    ).payload
    assert p["promo_type"] == "multibuy"
    assert p["promo_label"] == "2 for $5.00"
    assert p["promo_min_qty"] == 2
    assert p["price"] == p["regular_price"] == 21.05
    assert p["pack_price"] == p["regular_pack_price"] == 3.79


def test_no_promo_regular_equals_price_and_promo_fields_absent():
    p = _fields(name="Pams Colby Cheese 1kg", size="1kg", price=12.0, unit_raw="$12.00/1kg").payload
    assert p["regular_price"] == p["price"]
    assert p["regular_pack_price"] == p["pack_price"]
    for key in ("promo_type", "promo_label", "promo_ends", "promo_min_qty"):
        assert key not in p


def test_promo_ends_passes_through():
    p = _fields(
        name="Beef Mince 500g", size="500g", price=12.0, pack_price=9.0, regular_pack_price=12.0,
        promo_type="sale", promo_label="Special", promo_ends="2026-11-20", unit_raw="$24.00/1kg",
    ).payload
    assert p["promo_ends"] == "2026-11-20"


def test_promo_type_with_no_price_drop_is_ignored():
    p = _fields(
        name="Pams Colby Cheese 1kg", size="1kg", price=12.0, pack_price=12.0, regular_pack_price=12.0,
        promo_type="sale", promo_label="Special", unit_raw="$12.00/1kg",
    ).payload
    assert "promo_type" not in p


def test_facial_tissue_pk_is_sheet_count_only_when_arithmetic_fits():
    r = _fields(name="Sorbent Silky White Tissues 200pk", size="200pk", price=2.09, unit_raw="$1.05/100")
    p = r.payload
    assert (p["unit"], p["pack_count"], p["sheets_per_item"], p["total_sheets"]) == ("100sheets", 1, 200, 200)
    assert "tissue_pk_is_sheets" in r.notes


def test_facial_tissue_multipack_that_does_not_fit_stays_a_pack():
    p = _fields(name="Kleenex Tissues 4pk", size="4pk", price=9.0, unit_raw="$1.00/100").payload
    assert p["pack_count"] == 4
    assert p["unit"] == "100sheets"
    assert p["price"] == 1.0
    assert "total_sheets" not in p


def test_sheets_mismatch_falls_back_to_store_figure():
    r = _fields(name="Odd Toilet Paper", size="12 x 180 sheets", price=10.0, unit_raw="$0.13/100 sheets")
    p = r.payload
    assert p["price"] == 0.13
    assert "total_sheets" not in p
    assert any(n.startswith("sheets_mismatch") for n in r.notes)


def test_unparsed_bare_100_for_non_tissue_is_reported():
    p = _fields(name="Surf Laundry Capsules 50pk", size="50pk", price=18.59, unit_raw="$37.18/100").payload
    assert p["unit"] == "each"
    assert p["price"] == pytest.approx(0.3718)
    assert p["pack_count"] == 50
    assert p["unit_unparsed"] == "$37.18/100"


def test_legacy_snapshot_record_without_new_keys():
    p = _fields(name="Brown Onions", size="kg", price=3.49, unit_price=3.49, unit="kg").payload
    assert (p["price"], p["unit"], p["pack_price"], p["regular_price"]) == (3.49, "kg", 3.49, 3.49)
    p = _fields(name="Cucumber", size="ea", price=1.5, unit_price=None, unit="each").payload
    assert (p["price"], p["unit"], p["pack_count"]) == (1.5, "each", 1)


def test_legacy_record_with_pack_name_but_no_unit_price():
    p = _fields(name="Free Range Eggs 12pk", size="12pk", price=8.5, unit_price=None, unit=None).payload
    assert (p["unit"], p["pack_count"]) == ("each", 12)
    assert p["price"] == pytest.approx(0.7083, abs=0.001)


def test_wipes_subtitle_in_sheets_gives_the_count_when_arithmetic_fits():
    r = _fields(name="Sorbent Hypo-Allergenic Flushable Wipes", size="40 sheets", price=4.79, unit_raw="$0.12/ea")
    p = r.payload
    assert (p["price"], p["unit"], p["pack_price"], p["pack_count"]) == (0.12, "each", 4.79, 40)
    assert "unit_unparsed" not in p
    assert "sheets_as_count" in r.notes


def test_each_with_sheets_that_do_not_fit_is_not_used_as_count():
    r = _fields(name="Odd Thing", size="40 sheets", price=20.0, unit_raw="$0.12/ea")
    assert r.payload["pack_count"] == 1
