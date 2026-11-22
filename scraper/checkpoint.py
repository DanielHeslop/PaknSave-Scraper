"""Checkpoint of a category scrape in progress, so a restart does not lose the run.

A Pak'nSave run takes about two hours and holds everything in memory until the end.
If the Pi loses power part way, nothing is saved. After each finished category the
scrape writes data/checkpoints/<store>.json; the next run of the same store on the same
day carries on after the last finished category, and the file is removed once the
snapshot is written. A different day, or a changed category list, starts afresh.
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


def save(store: str, state: dict) -> None:
    path = checkpoint_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**state, "store": store, "day": date.today().isoformat()}))
    tmp.replace(path)


def load(store: str, urls: list[str]) -> dict | None:
    """The saved state of today's unfinished scrape of this category list, else None."""
    try:
        data = json.loads(checkpoint_path(store).read_text())
        if data["store"] != store or data["day"] != date.today().isoformat():
            return None
        if data["signature"] != signature(urls) or not 0 < data["done"] <= len(urls):
            return None
        data["page_stats"] = [
            PageStat(**{k: v for k, v in s.items() if k in _PAGE_STAT_FIELDS}) for s in data["page_stats"]
        ]
        data["scraped_categories"] = {k: tuple(v) for k, v in data["scraped_categories"].items()}
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def clear(store: str) -> None:
    try:
        checkpoint_path(store).unlink(missing_ok=True)
    except OSError:
        pass
