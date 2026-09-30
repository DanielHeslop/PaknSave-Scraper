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
import urllib.error

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


# --- scrape-exclusions filtering ---------------------------------------
#
# These use a MAILBOX_URL that actually ends in "price-mailbox" (unlike the
# tests above, which use "https://example.com/mailbox" and so never trigger
# a real exclusions lookup at all - see _exclusions_url()) so the derived
# exclusions URL ("https://example.com/api/scrape-exclusions") is exercised.

_TWO_PRODUCTS = [
    {
        "product_id": "P100",
        "name": "Keep Me",
        "size": "ea",
        "price": 2.00,
        "unit_price": None,
        "unit": "each",
        "supermarket": "New World",
    },
    {
        "product_id": "P200",
        "name": "Exclude Me",
        "size": "ea",
        "price": 3.00,
        "unit_price": None,
        "unit": "each",
        "supermarket": "New World",
    },
]


class _FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _run_publish(
    monkeypatch,
    products,
    excluded_keys=None,
    get_raises=None,
    get_body_override=None,
    dry_run=False,
):
    """Run publish_to_mailbox with a fake transport for both the GET
    (scrape-exclusions) and POST (mailbox) requests, returning
    (post_calls, printed_output).

    excluded_keys: list of keys the fake exclusions endpoint returns.
    get_raises: an exception instance the GET call should raise instead.
    get_body_override: raw bytes to return from the GET call instead of the
    normal {"excluded": [...]} shape (for malformed-response tests).
    """
    monkeypatch.setenv("MAILBOX_URL", "https://example.com/api/price-mailbox")
    monkeypatch.setenv("MAILBOX_TOKEN", "test-token")
    monkeypatch.delenv("EXCLUSIONS_URL", raising=False)

    post_calls = []

    if get_body_override is not None:
        get_body = get_body_override
    else:
        get_body = json.dumps(
            {"excluded": [{"key": k, "name": k, "at": "2026-10-01"} for k in (excluded_keys or [])]}
        ).encode("utf-8")

    def _fake_urlopen(request, timeout=None):
        if request.get_method() == "GET":
            assert request.get_full_url() == "https://example.com/api/scrape-exclusions"
            assert request.get_header("Authorization") == "Bearer test-token"
            if get_raises is not None:
                raise get_raises
            return _FakeResponse(body=get_body)
        post_calls.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr("scraper.publish.urllib.request.urlopen", _fake_urlopen)

    from scraper.publish import publish_to_mailbox as _publish

    _publish(products, dry_run=dry_run)
    return post_calls


def test_exclusions_removes_matching_keys(monkeypatch, capsys):
    post_calls = _run_publish(monkeypatch, _TWO_PRODUCTS, excluded_keys=["New World|P200"])
    assert len(post_calls) == 1
    ids = {item["product_id"] for item in post_calls[0]["items"]}
    assert ids == {"P100"}
    assert "Excluded 1 of 2 items per scrape-exclusions list" in capsys.readouterr().out


def test_exclusions_keeps_non_matching(monkeypatch, capsys):
    post_calls = _run_publish(monkeypatch, _TWO_PRODUCTS, excluded_keys=["Woolworths|P200"])
    ids = {item["product_id"] for item in post_calls[0]["items"]}
    assert ids == {"P100", "P200"}
    assert "Excluded 0 of 2 items per scrape-exclusions list" in capsys.readouterr().out


def test_exclusions_list_empty(monkeypatch, capsys):
    post_calls = _run_publish(monkeypatch, _TWO_PRODUCTS, excluded_keys=[])
    ids = {item["product_id"] for item in post_calls[0]["items"]}
    assert ids == {"P100", "P200"}
    assert "Excluded 0 of 2 items per scrape-exclusions list" in capsys.readouterr().out


def test_exclusions_fetch_failure_publishes_everything(monkeypatch, capsys):
    post_calls = _run_publish(
        monkeypatch, _TWO_PRODUCTS, get_raises=urllib.error.URLError("no network")
    )
    ids = {item["product_id"] for item in post_calls[0]["items"]}
    assert ids == {"P100", "P200"}
    out = capsys.readouterr().out
    assert "publishing everything" in out.lower()


def test_exclusions_bad_json_publishes_everything(monkeypatch, capsys):
    post_calls = _run_publish(monkeypatch, _TWO_PRODUCTS, get_body_override=b"not json{{{")
    ids = {item["product_id"] for item in post_calls[0]["items"]}
    assert ids == {"P100", "P200"}
    out = capsys.readouterr().out
    assert "publishing everything" in out.lower()


def test_exclusions_key_match_is_exact_not_prefix(monkeypatch, capsys):
    """A product_id that is a prefix of an excluded key's product_id (or
    vice versa) must not be treated as a match."""
    products = [
        {
            "product_id": "P10",
            "name": "Short Id",
            "size": "ea",
            "price": 1.00,
            "unit_price": None,
            "unit": "each",
            "supermarket": "New World",
        },
        {
            "product_id": "P100",
            "name": "Longer Id",
            "size": "ea",
            "price": 1.00,
            "unit_price": None,
            "unit": "each",
            "supermarket": "New World",
        },
    ]
    # Excludes only the longer id - "New World|P10" must not also match
    # "New World|P100" via a substring/prefix comparison.
    post_calls = _run_publish(monkeypatch, products, excluded_keys=["New World|P100"])
    ids = {item["product_id"] for item in post_calls[0]["items"]}
    assert ids == {"P10"}


def test_dry_run_does_not_post_reports_would_exclude(monkeypatch, capsys):
    post_calls = _run_publish(
        monkeypatch, _TWO_PRODUCTS, excluded_keys=["New World|P200"], dry_run=True
    )
    assert post_calls == []
    out = capsys.readouterr().out
    assert "Excluded 1 of 2 items per scrape-exclusions list" in out
    assert "Dry run: would send 1 prices to the app." in out


def test_no_token_in_log_output_on_any_path(monkeypatch, capsys):
    token = "test-token"
    for kwargs in (
        {"excluded_keys": ["New World|P200"]},
        {"get_raises": urllib.error.URLError("no network")},
        {"get_body_override": b"not json{{{"},
        {"excluded_keys": [], "dry_run": True},
    ):
        _run_publish(monkeypatch, _TWO_PRODUCTS, **kwargs)
        out = capsys.readouterr().out
        assert token not in out


if __name__ == "__main__":
    test_map_unit_label_kg_variants()
    test_map_unit_label_litre_variants()
    test_map_unit_label_each_variants()
    test_map_unit_label_leaves_unknown_and_per_100_unchanged()
    test_map_unit_label_empty_and_none_unchanged()
    print("All map_unit_label tests passed (run the rest via pytest - they need monkeypatch).")
