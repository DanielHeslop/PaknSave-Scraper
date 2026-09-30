"""Tests for publish.py's mailbox payload, in particular map_unit_label()
and the "unit_label" field added alongside it.

Context: New World mailbox items were showing "(kg)"/"(L)" suffixes in the
recipe app instead of "(per kg)"/"(per L)" like Pak'nSave/Woolworths. Real
snapshot data confirmed this repo's own "unit" field is the bare literal
"kg"/"L"/"each" for ALL THREE chains identically (see publish.py's
docstring) - so the fix here is a purely additive "unit_label" field for
the app to use for display, without changing "unit" itself for any chain.
"""

from __future__ import annotations

import json

from scraper.publish import map_unit_label, publish_to_mailbox


def test_map_unit_label_kg_variants():
    assert map_unit_label("kg") == "per kg"
    assert map_unit_label("per kg") == "per kg"
    assert map_unit_label("/kg") == "per kg"
    assert map_unit_label("kilo") == "per kg"
    assert map_unit_label("KG") == "per kg"


def test_map_unit_label_litre_variants():
    assert map_unit_label("L") == "per L"
    assert map_unit_label("per L") == "per L"
    assert map_unit_label("litre") == "per L"
    assert map_unit_label("liter") == "per L"


def test_map_unit_label_each_variants():
    assert map_unit_label("ea") == "ea"
    assert map_unit_label("each") == "ea"
    assert map_unit_label("unit") == "ea"


def test_map_unit_label_leaves_unknown_and_per_100_unchanged():
    assert map_unit_label("per 100g") == "per 100g"
    assert map_unit_label("500g") == "500g"


def test_map_unit_label_empty_and_none_unchanged():
    assert map_unit_label("") == ""
    assert map_unit_label(None) is None


def _capture_published_body(monkeypatch, products):
    """Run publish_to_mailbox with a fake urlopen, returning the decoded
    JSON body it would have sent."""
    monkeypatch.setenv("MAILBOX_URL", "https://example.com/mailbox")
    monkeypatch.setenv("MAILBOX_TOKEN", "test-token")

    captured = {}

    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse()

    monkeypatch.setattr("scraper.publish.urllib.request.urlopen", _fake_urlopen)
    publish_to_mailbox(products)
    return captured["body"]


def test_new_world_kg_item_gets_per_kg_unit_label(monkeypatch):
    products = [
        {
            "product_id": "P5045856",
            "name": "Brown Onions",
            "size": "kg",
            "price": 3.49,
            "unit_price": 3.49,
            "unit": "kg",
            "supermarket": "New World",
        }
    ]
    body = _capture_published_body(monkeypatch, products)
    item = body["items"][0]
    assert item["unit"] == "kg"  # unchanged
    assert item["unit_label"] == "per kg"


def test_paknsave_and_woolworths_unit_field_unchanged(monkeypatch):
    """The existing "unit" field must stay byte-identical for every chain -
    only the new "unit_label" field is added."""
    products = [
        {
            "product_id": "P5039965",
            "name": "Carrots",
            "size": "kg",
            "price": 1.99,
            "unit_price": 1.99,
            "unit": "kg",
            "supermarket": "Pak'nSave",
        },
        {
            "product_id": "135344",
            "name": "carrots",
            "size": "per kg",
            "price": 2.25,
            "unit_price": 2.25,
            "unit": "kg",
            "supermarket": "Woolworths",
        },
    ]
    body = _capture_published_body(monkeypatch, products)
    paknsave_item, woolworths_item = body["items"]
    assert paknsave_item["unit"] == "kg"
    assert paknsave_item["unit_label"] == "per kg"
    assert woolworths_item["unit"] == "kg"
    assert woolworths_item["unit_label"] == "per kg"


def test_each_item_unit_label_is_ea(monkeypatch):
    products = [
        {
            "product_id": "P1234567",
            "name": "Cucumber",
            "size": "ea",
            "price": 1.50,
            "unit_price": None,
            "unit": "each",
            "supermarket": "New World",
        }
    ]
    body = _capture_published_body(monkeypatch, products)
    item = body["items"][0]
    assert item["unit"] == "each"
    assert item["unit_label"] == "ea"


if __name__ == "__main__":
    test_map_unit_label_kg_variants()
    test_map_unit_label_litre_variants()
    test_map_unit_label_each_variants()
    test_map_unit_label_leaves_unknown_and_per_100_unchanged()
    test_map_unit_label_empty_and_none_unchanged()
    print("All map_unit_label tests passed (run the rest via pytest - they need monkeypatch).")
