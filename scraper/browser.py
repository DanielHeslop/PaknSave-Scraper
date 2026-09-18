"""Page navigation, retry logic, lazy-load triggering, and categories.txt parsing.

Read-only navigation only: this module only ever GETs category listing pages.
It never logs in, adds to a cart, or checks out.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

GOTO_TIMEOUT_MS = 8000
MAX_LOAD_ATTEMPTS = 3
LAZY_LOAD_SCROLL_PRESSES = 3
LAZY_LOAD_SCROLL_DELAY_SECONDS = 0.12
READY_WAIT_TIMEOUT_MS = 8000

# The store every PAK'nSAVE scrape is pinned to: PAK'nSAVE Petone, 114-124
# Jackson Street, Petone, Wellington, 5012. See README.md for why.
#
# Without this, PAK'nSAVE silently defaults new sessions to whatever store
# its own IP-based geolocation guesses (confirmed live: it picked PAK'nSAVE
# Royal Oak for this machine) - so which store's prices you get would
# depend on where the scraper happens to run from, not anything explicit.
#
# How this was determined: manually opened the site's own "Select a store
# to shop from" picker in a real stealth browser session, searched
# "Petone", clicked Select, and diffed context.cookies() before/after.
# The URL did not change and no geolocation permission was requested/used;
# two cookies changed value to the store's UUID: STORE_ID_V2 (paired with
# a "|False" suffix) and eCom_STORE_ID (bare UUID). Setting just these two
# cookies on a brand-new context - no click, no prior session - was then
# independently verified to reproduce "Your store is PAK'nSAVE Petone" on
# first page load, which is what new_pinned_context() below does.
PETONE_STORE_ID = "98ec3885-ac93-4fcb-807b-59c9055c52c4"
STORE_COOKIE_DOMAIN = "www.paknsave.co.nz"

# New World runs on the same shared Foodstuffs storefront as PAK'nSAVE -
# same STORE_ID_V2 / eCom_STORE_ID cookie-pinning mechanism, same
# data-testid page structure - confirmed live 2026-09-18 by fetching
# https://www.newworld.co.nz and diffing context.cookies() after the site's
# own IP-geolocation default kicked in (it picked "New World Metro Queen
# St" for this machine, same mechanism that made PAK'nSAVE default to
# Royal Oak). Store IDs are per-chain even though the cookie mechanism is
# shared - a PAK'nSAVE store GUID does not select a New World store.
#
# New World Island Bay, 6 Medway Street, Island Bay, Wellington, 6023 -
# confirmed via manual store-picker selection + cookie diff on 2026-09-18.
# No public GetStoreList-style endpoint was found for New World's current
# (Next.js) storefront - the old CommonApi path 404s and the newer
# api-prod.newworld.co.nz/v1/edge/store endpoint requires a JWT this
# scraper doesn't have - so, same as PETONE_STORE_ID above, this GUID was
# found manually: opened a stealth browser context against
# www.newworld.co.nz, clicked the header's "choose-store" button then its
# "Change store" tooltip button (which navigates to
# /shop/fulfillment?from=%2F - the site's own "Collect from" store
# picker), searched "Island Bay" in the "Search by store name, city or
# town/suburb" box, clicked "Select" on the "New World Island Bay, 6
# Medway Street, Island Bay, Wellington, 6023" result, and diffed
# context.cookies() before/after. The header's own store-name element
# (data-testid="choose-store") updated to "New World Island Bay" as
# on-page confirmation the selection took. Setting just these two cookies
# on a brand-new context - no click, no prior session - was then
# independently verified to reproduce "New World Island Bay" as the
# header's store name on first page load.
NEWWORLD_STORE_ID = "d4408e0f-5268-42c2-ba76-2bc9732d4316"
NEWWORLD_STORE_COOKIE_DOMAIN = "www.newworld.co.nz"


@dataclass(frozen=True)
class Site:
    """One supermarket chain's site config: which domain to pin a store on,
    which store GUID to pin it to, what to tag output with, and which
    categories file / snapshots subdirectory to use by default.
    """

    supermarket: str
    store_cookie_domain: str
    store_id: str
    default_categories_file: str
    default_snapshots_subdir: str


SITES: dict[str, Site] = {
    "paknsave": Site(
        supermarket="Pak'nSave",
        store_cookie_domain=STORE_COOKIE_DOMAIN,
        store_id=PETONE_STORE_ID,
        default_categories_file="categories.txt",
        default_snapshots_subdir="snapshots",
    ),
    "newworld": Site(
        supermarket="New World",
        store_cookie_domain=NEWWORLD_STORE_COOKIE_DOMAIN,
        store_id=NEWWORLD_STORE_ID,
        default_categories_file="newworld_categories.txt",
        default_snapshots_subdir="newworld_snapshots",
    ),
}


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


def stealth_playwright():
    """Start Playwright's async API with playwright-stealth applied.

    step0_verify.py confirmed this technique against the live site using
    the sync API: `Stealth().use_sync(sync_playwright())`. The real scraper
    (scraper/run.py) uses Playwright's async API throughout, so this uses
    that pattern's documented async counterpart instead:
    `Stealth().use_async(async_playwright())` - the other officially
    documented usage in the same playwright-stealth README, not a guess.

    Wrapping the context manager itself (rather than calling
    apply_stealth on a page after the fact) means every browser, context,
    and page opened through the returned object automatically gets stealth
    evasions applied - navigator.webdriver hidden, chrome.* runtime/plugin
    fingerprints spoofed, a real Chrome user-agent/sec-ch-ua substituted for
    headless Chrome's, etc. This is the only place the real scraper should
    ever call async_playwright() directly, so every browser session it
    opens goes through this.
    """
    return Stealth().use_async(async_playwright())


async def new_pinned_context(browser, store_id: str = PETONE_STORE_ID, domain: str = STORE_COOKIE_DOMAIN):
    """Create a browser context pinned to a specific store on a specific
    Foodstuffs-platform domain. Defaults to PAK'nSAVE Petone (see
    PETONE_STORE_ID above for how this was determined) so every existing
    call site is unaffected.

    Sets the two cookies the site's own store picker sets when a shopper
    manually selects a store, before any page is loaded, so every page
    this context navigates to reports the pinned store from the very first
    load - not just after some in-page interaction. Confirmed live for
    New World too (see NEWWORLD_STORE_ID above) - same two
    cookie names, same mechanism, just a different domain/store GUID.
    """
    context = await browser.new_context()
    await context.add_cookies(
        [
            {
                "name": "STORE_ID_V2",
                "value": f"{store_id}|False",
                "domain": domain,
                "path": "/",
            },
            {
                "name": "eCom_STORE_ID",
                "value": store_id,
                "domain": domain,
                "path": "/",
            },
        ]
    )
    return context


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
