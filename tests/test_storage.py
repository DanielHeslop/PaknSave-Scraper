"""Tests for scraper/storage.py's compound-key price_history.json scheme -
new test module because this data format had no coverage before, and it's
correctness-critical (a bug here means one chain's scrape can silently
overwrite another's price history).

All tests operate on tmp_path files only - never the real
data/price_history.json.
"""

from __future__ import annotations

import json

from scraper.storage import (
    HISTORY_KEY_SEPARATOR,
    LEGACY_DEFAULT_SUPERMARKET,
    load_price_history,
    update_price_history,
)


def _product(product_id: str, supermarket: str, price: float, scraped_at: str = "2026-09-18") -> dict:
    return {
        "product_id": product_id,
        "name": f"Test product {product_id}",
        "category": "test",
        "size": "1kg",
        "price": price,
        "unit_price": price,
        "unit": "kg",
        "scraped_at": scraped_at,
        "supermarket": supermarket,
    }


def test_new_entries_keyed_by_supermarket_and_product_id(tmp_path):
    history_path = tmp_path / "price_history.json"
    update_price_history([_product("P5040098", "Pak'nSave", 9.99)], history_path)

    history = json.loads(history_path.read_text())
    assert list(history.keys()) == [f"Pak'nSave{HISTORY_KEY_SEPARATOR}P5040098"]


def test_same_product_id_different_chains_does_not_collide(tmp_path):
    """The exact collision this fix prevents: Pak'nSave and New World both
    derive product IDs from the same shared Foodstuffs image CDN, so the
    same raw ID can mean two different products on two different chains.
    """
    history_path = tmp_path / "price_history.json"

    update_price_history([_product("P5040098", "Pak'nSave", 9.99)], history_path)
    update_price_history([_product("P5040098", "New World", 11.49)], history_path)

    history = json.loads(history_path.read_text())
    assert history["Pak'nSave|P5040098"][-1]["price"] == 9.99
    assert history["New World|P5040098"][-1]["price"] == 11.49
    assert len(history) == 2


def test_legacy_bare_keys_migrate_to_compound_keys(tmp_path):
    history_path = tmp_path / "price_history.json"
    legacy_entry = [{"date": "2026-09-07", "price": 1.99, "unit_price": 1.99, "unit": "kg"}]
    history_path.write_text(json.dumps({"P5045856": legacy_entry}))

    migrated = load_price_history(history_path)

    expected_key = f"{LEGACY_DEFAULT_SUPERMARKET}{HISTORY_KEY_SEPARATOR}P5045856"
    assert expected_key in migrated
    assert "P5045856" not in migrated
    # Every field of the entry itself must be byte-identical - only the key changed.
    assert migrated[expected_key] == legacy_entry

    # Migration is persisted to disk immediately.
    on_disk = json.loads(history_path.read_text())
    assert on_disk == migrated


def test_migration_backup_created_once(tmp_path):
    history_path = tmp_path / "price_history.json"
    original = {"P5045856": [{"date": "2026-09-07", "price": 1.99, "unit_price": 1.99, "unit": "kg"}]}
    history_path.write_text(json.dumps(original))

    load_price_history(history_path)

    backup_path = tmp_path / "price_history.json.pre-compound-key-backup"
    assert backup_path.exists()
    assert json.loads(backup_path.read_text()) == original


def test_migration_is_idempotent_second_run_is_a_noop(tmp_path):
    history_path = tmp_path / "price_history.json"
    history_path.write_text(
        json.dumps({"P5045856": [{"date": "2026-09-07", "price": 1.99, "unit_price": 1.99, "unit": "kg"}]})
    )

    first = load_price_history(history_path)
    after_first_migration = history_path.read_text()

    second = load_price_history(history_path)
    after_second_load = history_path.read_text()

    assert first == second
    assert after_first_migration == after_second_load


def test_already_migrated_file_has_no_legacy_keys_left(tmp_path):
    history_path = tmp_path / "price_history.json"
    history_path.write_text(json.dumps({"P5045856": []}))

    load_price_history(history_path)
    history = load_price_history(history_path)

    assert all(HISTORY_KEY_SEPARATOR in key for key in history)
