"""Checkpoint of a scrape in progress, so a restart does not lose the run.

A run takes up to two hours and holds everything in memory until the end. If the Pi
loses power part way, nothing is saved. After each finished category (Pak'nSave, New
World) or shelf and search term (Woolworths) the scrape writes data/checkpoints/<store>.json;
the next run of the same store on the same day carries on after the last finished step,
and the file is removed once the snapshot is written. A different day, a changed list, a
damaged file or a dry run starts afresh.

Pak'nSave / New World keep one flat record (save / load). Woolworths keeps a named section
per step (save_section / load_section), each with the signature of the list it was made for.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import date
from pathlib import Path

from scraper import category_tree
from scraper.page_limits import PageStat

_PAGE_STAT_FIELDS = {f.name for f in fields(PageStat)}


def checkpoint_path(store: str) -> Path:
    return Path(category_tree.STATE_DIR) / "checkpoints" / f"{store}.json"


def signature(urls: list[str]) -> str:
    return "\n".join(urls)


def _write(store: str, data: dict) -> None:
    path = checkpoint_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**data, "store": store, "day": date.today().isoformat()}))
    tmp.replace(path)


def read_today(store: str) -> dict | None:
    """The store's checkpoint file if it is readable and was written today, else None."""
    try:
        data = json.loads(checkpoint_path(store).read_text())
        if not isinstance(data, dict) or data["store"] != store or data["day"] != date.today().isoformat():
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def exists_today(store: str) -> bool:
    """True when a run of this store was cut off earlier today (its progress file is still there)."""
    return read_today(store) is not None


def clear(store: str) -> None:
    try:
        checkpoint_path(store).unlink(missing_ok=True)
    except OSError:
        pass


def save(store: str, state: dict) -> None:
    _write(store, state)


def load(store: str, urls: list[str]) -> dict | None:
    """The saved state of today's unfinished scrape of this category list, else None."""
    data = read_today(store)
    try:
        if data is None or data["signature"] != signature(urls) or not 0 < data["done"] <= len(urls):
            return None
        data["page_stats"] = page_stats_from_records(data["page_stats"])
        data["scraped_categories"] = {k: tuple(v) for k, v in data["scraped_categories"].items()}
        return data
    except (KeyError, TypeError, AttributeError, ValueError):
        return None


def page_stats_from_records(records: list[dict]) -> list[PageStat]:
    return [PageStat(**{k: v for k, v in s.items() if k in _PAGE_STAT_FIELDS}) for s in records]


def save_section(store: str, name: str, sig: str, state: dict) -> None:
    data = read_today(store) or {}
    sections = data.get("sections") if isinstance(data.get("sections"), dict) else {}
    sections[name] = {"signature": sig, "state": state}
    _write(store, {"sections": sections})


def load_section(store: str, name: str, sig: str) -> dict | None:
    """One step's saved state, if it was made today for this same list (signature)."""
    data = read_today(store)
    try:
        section = data["sections"][name]
        return section["state"] if section["signature"] == sig and isinstance(section["state"], dict) else None
    except (KeyError, TypeError):
        return None
