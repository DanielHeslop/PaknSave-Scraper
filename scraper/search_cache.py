"""Track which ingredient names were recently found, to avoid re-searching them.

data/search_cache.json is shaped as:

    { "ingredient name": "date last successfully found" }

keyed by the ingredient's own name text, as it appears in the recipe app's
ingredient list - NOT by product ID. This is deliberately a separate file
from data/price_history.json, which is keyed by product ID and built from
category-page scraping. Searching and category-scraping track freshness of
two different things (an ingredient name vs. a specific product) and
conflating the two files would make both harder to reason about.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

DEFAULT_STALE_AFTER_DAYS = 7


def load_search_cache(path: str | Path) -> dict[str, str]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_search_cache(cache: dict[str, str], path: str | Path) -> None:
    Path(path).write_text(json.dumps(cache, indent=2, sort_keys=True))


def was_recently_found(
    ingredient_name: str,
    cache: dict[str, str],
    today: date,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
) -> bool:
    """True if ingredient_name was found within the last stale_after_days.

    An unparsable or missing date is treated as "not recently found" (i.e.
    search it again) rather than guessed at.
    """
    last_found = cache.get(ingredient_name)
    if not last_found:
        return False
    try:
        last_found_date = date.fromisoformat(last_found)
    except ValueError:
        return False
    return (today - last_found_date) < timedelta(days=stale_after_days)
