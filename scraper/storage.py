"""Writing scraped results to disk.

Two files per run:
  - data/snapshots/<date>.json - every product from this run, including
    ones with blank fields, for a complete point-in-time record. A plain
    list, not keyed by product_id, so there's no cross-chain collision risk
    here - each chain already writes its own dated file to its own
    directory (data/snapshots/, data/woolworths_snapshots/, ...).
  - data/price_history.json - an append-only history, one new entry per
    product only when its price actually changed (or it's new), so it
    doesn't fill up with duplicate entries when nothing changed. Keyed by
    a compound "<supermarket>|<product_id>" key (see HISTORY_KEY_SEPARATOR
    below) - product IDs are NOT globally unique across chains (Pak'nSave
    and New World both derive theirs from the same shared Foodstuffs image
    CDN, so the same numeric ID can mean two different products on two
    different chains) - a bare product_id key would let one chain's scrape
    silently overwrite another's history for what the system would
    wrongly treat as "the same" product.
"""

from __future__ import annotations

import json
from pathlib import Path

# Separator between supermarket and product_id in a price_history.json key.
# Safe because no real product_id this codebase produces contains "|"
# (Pak'nSave/New World: "P" + digits; Woolworths: a bare numeric SKU
# string), and no supermarket name in use ("Pak'nSave", "New World",
# "Woolworths") contains it either - unlike ":" or "-", which could
# plausibly show up in a future chain's own product ID scheme.
HISTORY_KEY_SEPARATOR = "|"

# What a pre-existing bare-product_id key (written before this compound-key
# scheme existed) is assumed to mean when migrated - every entry in the
# wild predates multi-chain support and was written by the Pak'nSave
# scrape, the same assumption scraper/publish.py already makes for
# untagged records (`p.get("supermarket", "Pak'nSave")`).
LEGACY_DEFAULT_SUPERMARKET = "Pak'nSave"

# Suffix for the one-time pre-migration backup file, e.g.
# data/price_history.json.pre-compound-key-backup.
BACKUP_SUFFIX = ".pre-compound-key-backup"


def _history_key(product: dict) -> str:
    """The compound key a product's history entry is stored under."""
    return f"{product['supermarket']}{HISTORY_KEY_SEPARATOR}{product['product_id']}"


def _migrate_legacy_keys(history: dict, history_path: Path) -> tuple[dict, bool]:
    """Rewrite any bare product_id key (no HISTORY_KEY_SEPARATOR - meaning
    it predates the compound-key scheme) to
    "LEGACY_DEFAULT_SUPERMARKET|<product_id>", in place.

    Idempotent: a file with no legacy keys (freshly migrated, or one that
    never had any) is returned unchanged and the second return value is
    False, so the caller knows whether a write-back is needed. Writes a
    one-time backup of the pre-migration file before changing anything -
    but only when there's something to migrate, and only if a backup
    doesn't already exist (so a second migration attempt after a prior
    partial failure never overwrites the true original with
    already-half-migrated data).
    """
    legacy_keys = [k for k in history if HISTORY_KEY_SEPARATOR not in k]
    if not legacy_keys:
        return history, False

    backup_path = Path(str(history_path) + BACKUP_SUFFIX)
    if not backup_path.exists():
        backup_path.write_text(json.dumps(history, indent=2))

    for key in legacy_keys:
        new_key = f"{LEGACY_DEFAULT_SUPERMARKET}{HISTORY_KEY_SEPARATOR}{key}"
        # Every field of the entry itself is carried over completely
        # unchanged - only the key it's stored under changes.
        history[new_key] = history.pop(key)

    return history, True


def write_snapshot(products: list[dict], snapshots_dir: str | Path, date: str) -> Path:
    snapshots_dir = Path(snapshots_dir)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshots_dir / f"{date}.json"
    snapshot_path.write_text(json.dumps(products, indent=2))
    return snapshot_path


def load_price_history(history_path: str | Path) -> dict[str, list[dict]]:
    """Load price_history.json, migrating it to the compound-key format
    first if it's still in the old bare-product_id format (see
    _migrate_legacy_keys). The migration is persisted back to disk
    immediately, right here, so it only ever happens once per file -
    every subsequent load (from this function or update_price_history)
    finds no legacy keys left and is a no-op.
    """
    history_path = Path(history_path)
    if not history_path.exists():
        return {}
    try:
        history = json.loads(history_path.read_text())
    except json.JSONDecodeError:
        return {}

    history, migrated = _migrate_legacy_keys(history, history_path)
    if migrated:
        history_path.write_text(json.dumps(history, indent=2))

    return history


def update_price_history(
    products: list[dict], history_path: str | Path
) -> tuple[dict[str, list[dict]], int]:
    """Append a new history entry for each product whose price (or unit
    price) differs from its last recorded entry, or that has never been
    seen before. Returns (updated_history, number_of_new_entries_added).
    """
    history = load_price_history(history_path)
    new_entries = 0

    for product in products:
        key = _history_key(product)
        entry = {
            "date": product["scraped_at"],
            "price": product["price"],
            "unit_price": product["unit_price"],
            "unit": product["unit"],
        }

        existing = history.get(key, [])
        last = existing[-1] if existing else None

        changed = last is None or (
            last.get("price") != entry["price"]
            or last.get("unit_price") != entry["unit_price"]
        )

        if changed:
            existing.append(entry)
            history[key] = existing
            new_entries += 1

    Path(history_path).write_text(json.dumps(history, indent=2))
    return history, new_entries
