"""Woolworths item parsing for unit text, pack data and promo fields, plus the
sheet-count enrichment from product descriptions."""

from __future__ import annotations

from datetime import date

from scraper.item_fields import build_item_fields
from scraper.woolworths_sheets import enrich_sheet_counts
from scraper.woolworths_transport import _parse_item


def _item(name="purex toilet paper", sku="1", sale=9.4, original=None, cup=0.39, cup_list=None,
          measure="100ss", volume="6pack", package_type=None, club=False, special=False,
          tag=None, ends=None, multi=None):
    original = sale if original is None else original
    product_tag = tag
    if multi:
        product_tag = dict(tag or {}, multiBuy=multi)
    return {
        "sku": sku, "name": name, "unit": "Each",
        "price": {"originalPrice": original, "salePrice": sale, "isClubPrice": club,
                  "isSpecial": special, "promotionEndDate": ends},
        "size": {"cupListPrice": cup if cup_list is None else cup_list, "cupPrice": cup,
                 "cupMeasure": measure, "packageType": package_type, "volumeSize": volume},
        "productTag": product_tag,
        "departments": [{"id": 12, "name": "Household"}],
    }


def _parse(**kw):
    product, reason = _parse_item(_item(**kw), "household")
    assert reason is None
    return product


def test_100ss_cup_measure_becomes_100sheets():
    p = _parse(cup=0.39, measure="100ss")
    assert (p["unit"], p["unit_price"]) == ("100sheets", 0.39)


def test_100ea_wipes_cup_measure_becomes_each():
    p = _parse(name="huggies baby wipes", cup=4.16, measure="100ea", sale=6.65, volume="160pack")
    assert p["unit"] == "each"
    assert p["unit_price"] == 0.0416


def test_kg_cup_measure_unchanged():
    p = _parse(name="carrots", cup=2.25, measure="1kg", sale=2.25, volume="per kg")
    assert (p["unit"], p["unit_price"], p["price"]) == ("kg", 2.25, 2.25)


def test_40pk_flow_with_sheet_count():
    p = _parse(name="purex mega rolls", sale=10.39, cup=0.14, volume="40pack")
    p["sheets_per_item"] = 180
    f = build_item_fields(p).payload
    assert f["price"] == 0.1443 and f["unit"] == "100sheets"
    assert (f["pack_price"], f["pack_count"], f["total_sheets"]) == (10.39, 40, 7200)


def test_4pk_flow_without_sheet_count():
    p = _parse(name="cotton softs", sale=1.89, cup=0.26, volume=None, package_type="4 pack")
    f = build_item_fields(p).payload
    assert (f["price"], f["unit"], f["pack_price"], f["pack_count"]) == (0.26, "100sheets", 1.89, 4)
    assert "total_sheets" not in f


def test_special_sale_price_below_original():
    p = _parse(name="quilton toilet tissue", sale=9.99, original=13.5, cup=0.22, cup_list=0.30,
               volume="8pack", special=True, tag={"tagType": "IsSpecial"})
    assert p["promo_type"] == "sale" and p["promo_label"] == "Special"
    assert p["price"] == 9.99 and p["regular_pack_price"] == 13.5
    f = build_item_fields(p).payload
    assert (f["price"], f["regular_price"]) == (0.22, 0.30)
    assert (f["pack_price"], f["regular_pack_price"]) == (9.99, 13.5)
    assert f["pack_count"] == 8


def test_club_price_is_member():
    p = _parse(name="paseo luxury", sale=16.7, original=17.75, club=True, cup=0.40, cup_list=0.43,
               volume="24pack", tag={"tagType": "IsClubPrice"})
    assert p["promo_type"] == "member"
    assert p["promo_label"] == "Club price"


def test_great_price_label():
    p = _parse(sale=8.0, original=9.0, cup=0.2, cup_list=0.25, tag={"tagType": "IsGreatPrice"})
    assert p["promo_type"] == "sale" and p["promo_label"] == "Great price"


def test_end_date_is_kept_when_shown():
    p = _parse(sale=8.0, original=9.0, ends="2026-11-24T23:59:00Z", special=True)
    assert p["promo_ends"] == "2026-11-24"


def test_multibuy_when_tag_present():
    p = _parse(name="anchor yoghurt", sale=3.79, cup=2.11, measure="100g", volume="180g",
               multi={"text": "2 for $5"})
    assert p["promo_type"] == "multibuy"
    assert p["promo_min_qty"] == 2
    f = build_item_fields(p).payload
    assert f["price"] == f["regular_price"]
    assert f["pack_price"] == f["regular_pack_price"] == 3.79


def test_no_promo():
    p = _parse(sale=9.4, original=9.4)
    assert "promo_type" not in p
    f = build_item_fields(p).payload
    assert f["regular_price"] == f["price"]
    assert f["regular_pack_price"] == f["pack_price"] == 9.4


# --- sheet-count enrichment ------------------------------------------------


def _tp(sku, name="purex toilet paper", unit="100sheets", size="40pack"):
    return {"product_id": sku, "name": name, "size": size, "unit": unit}


def test_enrich_sets_sheet_count_and_caches(tmp_path):
    cache = tmp_path / "c.json"
    calls = []

    def fetch(sku):
        calls.append(sku)
        return "<p>Soft rolls, 180 sheets per roll.</p>"

    products = [_tp("1")]
    n = enrich_sheet_counts(products, fetch, str(cache), save_cache=True, log=lambda *_: None,
                            today=date(2026, 11, 12))
    assert n == 1 and products[0]["sheets_per_item"] == 180
    again = [_tp("1")]
    n = enrich_sheet_counts(again, fetch, str(cache), save_cache=True, log=lambda *_: None,
                            today=date(2026, 11, 13))
    assert n == 0 and calls == ["1"] and again[0]["sheets_per_item"] == 180


def test_enrich_ignores_ambiguous_text(tmp_path):
    products = [_tp("1")]
    enrich_sheet_counts(products, lambda s: "Mega Roll 400 sheets vs our standard 170 sheet roll",
                        str(tmp_path / "c.json"), log=lambda *_: None)
    assert "sheets_per_item" not in products[0]


def test_enrich_skips_non_sheet_products_and_known_sheet_counts(tmp_path):
    products = [_tp("1", unit="kg"), _tp("2", size="12 x 180 sheets")]
    calls = []
    enrich_sheet_counts(products, lambda s: calls.append(s), str(tmp_path / "c.json"), log=lambda *_: None)
    assert calls == []


def test_enrich_failed_lookup_is_skipped_and_not_cached(tmp_path):
    cache = tmp_path / "c.json"

    def boom(sku):
        raise RuntimeError("down")

    products = [_tp("1")]
    enrich_sheet_counts(products, boom, str(cache), save_cache=True, log=lambda *_: None)
    assert "sheets_per_item" not in products[0]
    assert not cache.exists()


def test_enrich_respects_limit(tmp_path):
    products = [_tp(str(i)) for i in range(5)]
    calls = []
    n = enrich_sheet_counts(products, lambda s: calls.append(s) or None, str(tmp_path / "c.json"),
                            limit=2, log=lambda *_: None)
    assert n == 2 and len(calls) == 2
