"""Writing scraped results to disk.

Two files per run:
  - data/snapshots/<date>.json - every product from this run, including
    ones with blank fields, for a complete point-in-time record.
  - data/price_history.json - an append-only history, one new entry per
    product only when its price actually changed (or it's new), so it
    doesn't fill up with duplicate entries when nothing changed.
"""

from __future__ import annotations

import json
from pathlib import Path


def write_snapshot(products: list[dict], snapshots_dir: str | Path, date: str) -> Path:
    snapshots_dir = Path(snapshots_dir)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshots_dir / f"{date}.json"
    snapshot_path.write_text(json.dumps(products, indent=2))
    return snapshot_path


def load_price_history(history_path: str | Path) -> dict[str, list[dict]]:
    history_path = Path(history_path)
    if not history_path.exists():
        return {}
    try:
        return json.loads(history_path.read_text())
    except json.JSONDecodeError:
        return {}


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
        product_id = product["product_id"]
        entry = {
            "date": product["scraped_at"],
            "price": product["price"],
            "unit_price": product["unit_price"],
            "unit": product["unit"],
        }

        existing = history.get(product_id, [])
        last = existing[-1] if existing else None

        changed = last is None or (
            last.get("price") != entry["price"]
            or last.get("unit_price") != entry["unit_price"]
        )

        if changed:
            existing.append(entry)
            history[product_id] = existing
            new_entries += 1

    Path(history_path).write_text(json.dumps(history, indent=2))
    return history, new_entries
