"""Page limits: spot categories cut short by their page limit, read on, and remember.

A category is "cut short" when its last page at the page limit was full and there
were more products past it. A full page at the limit is the sign; the scrapers then
keep reading until a page is short or empty, up to SAFETY_CAP_PAGES. If that finds
more products the category was cut short, and the
page count found is remembered in data/page_counts/<store>.json. Later runs use
the larger of the categories file's `pages=N` and the remembered count as the
limit, so they read the right number of pages without probing.

Each run's per-category record (pages read, items on the last page, last page full)
is written, on a real run only, to data/page_stats/<store>/<date>.json.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from scraper import category_tree

SAFETY_CAP_PAGES = 20

# Why a category's paging ended.
SHORT_PAGE = "short page"
EMPTY_PAGE = "empty page"
REPEAT_PAGE = "repeat page"
SAFETY_CAP = "safety cap"
LOAD_FAILED = "load failed"
ROBOTS = "robots.txt"
ALL_READ = "all read"


@dataclass
class PageStat:
    label: str
    path: str
    configured: int  # pages= in the categories file (or the mapped default)
    limit: int  # the page limit in force this run: the larger of configured and remembered
    pages_read: int = 0  # pages that held products
    last_page_items: int = 0
    last_page_full: bool = False
    items: int = 0  # distinct products read across the pages
    full_at_limit: bool = False  # the page at the limit was full
    ended: str = ""

    @property
    def cut_short(self) -> bool:
        """The page at the limit was full and reading on found more products."""
        return self.full_at_limit and self.pages_read > self.limit

    @property
    def extra_pages(self) -> int:
        return self.pages_read - self.limit if self.cut_short else 0

    def record(self) -> dict:
        return {**asdict(self), "cut_short": self.cut_short, "extra_pages": self.extra_pages}


def note_page(stat: PageStat, page_number: int, items: int, new_items: int, full: bool) -> None:
    """Record a page that held products (a page with nothing new is not counted)."""
    stat.pages_read = page_number
    stat.last_page_items = items
    stat.last_page_full = full
    stat.items += new_items
    if full and page_number == stat.limit:
        stat.full_at_limit = True


def cut_short_line(stat: PageStat) -> str:
    return f"{stat.label}: limit {stat.limit} reached, read {stat.pages_read} pages, {stat.items} items" + (
        f" (stopped at the safety cap of {SAFETY_CAP_PAGES} pages)" if stat.ended == SAFETY_CAP else ""
    )


def log_cut_short(stats: list[PageStat], log) -> None:
    for stat in stats:
        if stat.cut_short:
            log(cut_short_line(stat))


def cut_short_counts(stats: list[PageStat]) -> dict:
    """{"cut_short": categories cut short, "extra_pages": pages read beyond the limits}."""
    return {
        "cut_short": sum(1 for s in stats if s.cut_short),
        "extra_pages": sum(s.extra_pages for s in stats),
    }


# --- remembered page counts -----------------------------------------------------

def counts_path(store: str) -> Path:
    return Path(category_tree.STATE_DIR) / "page_counts" / f"{store}.json"


def load_page_counts(store: str) -> dict[str, int]:
    """Category path -> pages it needs, as remembered from earlier real runs."""
    try:
        data = json.loads(counts_path(store).read_text())
        return {
            path: int(entry["pages"])
            for path, entry in data.get("counts", {}).items()
            if isinstance(entry, dict) and int(entry.get("pages", 0)) > 0
        }
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    tmp.replace(path)


def remember_page_counts(store: str, stats: list[PageStat]) -> int:
    """Update the remembered counts from a real run. Returns how many entries changed.

    A category that ended at its real end is remembered at the pages it held, when that is
    more than the categories file asks for (and forgotten when it is not). One that ended on
    a failed load or the safety cap is only ever raised, never lowered.
    """
    try:
        data = json.loads(counts_path(store).read_text())
        counts = {p: e for p, e in data.get("counts", {}).items() if isinstance(e, dict)}
    except (OSError, ValueError, AttributeError):
        counts = {}
    changed = 0
    now = datetime.now(timezone.utc).isoformat()
    for stat in stats:
        if not stat.pages_read or not stat.path:
            continue
        before = counts.get(stat.path, {}).get("pages")
        unsure = stat.ended in (LOAD_FAILED, ROBOTS, SAFETY_CAP)
        pages = stat.pages_read if not unsure else max(stat.pages_read, before or 0)
        if pages > stat.configured:
            if before != pages:
                counts[stat.path] = {"label": stat.label, "pages": pages, "items": stat.items, "updatedAt": now}
                changed += 1
        elif before is not None and not unsure:
            del counts[stat.path]
            changed += 1
    if changed:
        _write_json(counts_path(store), {"store": store, "counts": counts})
    return changed


# --- per-run record ---------------------------------------------------------

def stats_path(store: str, day: str | None = None) -> Path:
    return Path(category_tree.STATE_DIR) / "page_stats" / store / f"{day or date.today().isoformat()}.json"


def write_run_stats(store: str, stats: list[PageStat], day: str | None = None) -> Path:
    path = stats_path(store, day)
    _write_json(path, {"store": store, "categories": [s.record() for s in stats]})
    return path


def save_page_stats(store: str, stats: list[PageStat], log) -> None:
    """Write the run's record and remember the page counts. Real runs only; never raises."""
    try:
        path = write_run_stats(store, stats)
        changed = remember_page_counts(store, stats)
        log(f"Page limits: record written to {path}; {changed} remembered page count(s) changed.")
    except Exception as e:
        log(f"Page limits ({store}) could not be saved ({type(e).__name__}: {e}) - carrying on.")
