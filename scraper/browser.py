"""Page navigation, retry logic, lazy-load triggering, and categories.txt parsing.

Read-only navigation only: this module only ever GETs category listing pages.
It never logs in, adds to a cart, or checks out.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

GOTO_TIMEOUT_MS = 8000
MAX_LOAD_ATTEMPTS = 3
LAZY_LOAD_SCROLL_PRESSES = 3
LAZY_LOAD_SCROLL_DELAY_SECONDS = 0.12
READY_WAIT_TIMEOUT_MS = 8000


@dataclass
class CategoryPage:
    url: str
    category: str


def parse_categories_file(path: str | Path) -> list[CategoryPage]:
    """Parse categories.txt into one CategoryPage per page to be scraped.

    Each non-comment line is a category listing URL plus optional
    'pages=N' (how many paginated pages to also scrape, using PAK'nSAVE's
    own ?pg= query parameter) and 'category=name' (override the category
    label used in output). Blank lines and lines starting with # are
    ignored.
    """
    pages: list[CategoryPage] = []
    path = Path(path)
    if not path.exists():
        return pages

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        url = parts[0]
        num_pages = 1
        category = _derive_category_from_url(url)

        for part in parts[1:]:
            if part.startswith("pages="):
                try:
                    num_pages = max(1, int(part.split("=", 1)[1]))
                except ValueError:
                    num_pages = 1
            elif part.startswith("category="):
                category = part.split("=", 1)[1]

        base_url = url.split("?")[0]
        for page_num in range(1, num_pages + 1):
            if page_num == 1:
                pages.append(CategoryPage(url=url, category=category))
            else:
                pages.append(
                    CategoryPage(url=f"{base_url}?pg={page_num}", category=category)
                )

    return pages


def _derive_category_from_url(url: str) -> str:
    without_query = url.split("?")[0]
    return without_query.rstrip("/").split("/")[-1] or "uncategorised"


class PageLoadTimeout(Exception):
    """Raised when a page never becomes ready to read within the retry budget."""


async def navigate_and_wait_ready(page, url: str, log) -> None:
    """Navigate to url, trigger lazy-loaded content, and wait until it's
    safe to start reading - mirroring the reference project's approach.

    Waiting for a specific price element to appear (rather than a generic
    "page loaded" event) is the actual readiness signal, since prices are
    rendered by client-side JavaScript after the initial page load.

    Raises PageLoadTimeout if this doesn't succeed within MAX_LOAD_ATTEMPTS.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_LOAD_ATTEMPTS + 1):
        try:
            await page.goto(url, timeout=GOTO_TIMEOUT_MS)

            # Trigger lazy-loaded content by scrolling, same as the
            # reference project. asyncio.sleep (not time.sleep) so this
            # only pauses this one page's scrape, not the whole program.
            for _ in range(LAZY_LOAD_SCROLL_PRESSES):
                await page.keyboard.press("PageDown")
                await asyncio.sleep(LAZY_LOAD_SCROLL_DELAY_SECONDS)

            # Wait for the actual readiness signal: at least one price
            # element rendered, not just "DOM loaded".
            price_locator = page.get_by_test_id("price-dollars").last
            await price_locator.wait_for(state="visible", timeout=READY_WAIT_TIMEOUT_MS)
            return
        except Exception as e:
            last_error = e
            if attempt < MAX_LOAD_ATTEMPTS:
                log(f"Retrying page load {attempt}/{MAX_LOAD_ATTEMPTS}...")

    raise PageLoadTimeout(f"{url} - {last_error}")
