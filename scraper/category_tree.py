"""Each store's category tree: build it, cache it, and send it to the recipe app.

Pak'nSave / New World: the tree comes from the site's own category sitemap
(ecom_sitemap_categories.xml, found through the sitemap index that robots.txt
names). Woolworths: from the navigation the site itself loads (/api/v1/shell:
departments, aisles, shelves). Only paths robots.txt allows are kept.

A category is {"path", "name", "parent"}:
  - path   Foodstuffs: "pantry/oil--vinegar/cooking-oil" (the part of the URL after
           /shop/category/); Woolworths: "pantry/baking/flour" (department/aisle/shelf,
           the same form woolworths_categories.txt uses).
  - name   Woolworths: the site's own label. Foodstuffs: the last URL slug read as a
           title ("--" is "&"), because the sitemap carries no labels.
  - parent the path of the parent category, "" for a top-level one.

The tree is cached under data/category_trees/<store>.json together with a hash of
what was last posted, so the app is only sent a tree that changed. Posting is
POST <origin of MAILBOX_URL>/api/store-categories with Bearer MAILBOX_TOKEN; any
failure is logged (never with the token) and never stops a run.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from scraper.auth import bearer
from scraper.browser import PAGE_DELAY_JITTER_SECONDS, PAGE_DELAY_SECONDS, SITES
from scraper.robots import USER_AGENT, RobotsGuard, RobotsRules

STORES = ("paknsave", "newworld", "woolworths")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(REPO_ROOT, "data")

FOODSTUFFS_CATEGORY_PREFIX = "/shop/category/"
WOOLWORTHS_BROWSE_PREFIX = "/shop/browse/"
POST_PATH = "/api/store-categories"
POST_TIMEOUT_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 30
MAILBOX_URL_ENV = "MAILBOX_URL"
MAILBOX_TOKEN_ENV = "MAILBOX_TOKEN"


def log(message: str) -> None:
    print(message, flush=True)


@dataclass(frozen=True)
class Category:
    path: str
    name: str
    parent: str = ""

    def to_json(self) -> dict:
        return {"path": self.path, "name": self.name, "parent": self.parent}


def categories_from_json(items) -> list[Category]:
    return [Category(c["path"], c["name"], c.get("parent", "")) for c in items]


def tree_hash(categories: list[Category]) -> str:
    body = json.dumps([c.to_json() for c in sorted(categories, key=lambda c: c.path)], sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --- names ---------------------------------------------------------------

_SMALL_WORDS = {"and", "of", "to", "with", "in", "for", "the", "a"}


def slug_to_name(slug: str) -> str:
    """A readable name from a Foodstuffs URL slug.

    "pasta-rice--noodles" -> "Pasta Rice & Noodles"; "cr_me" -> "Crème";
    "men_s" -> "Men's"; "_6-12-months_" -> "(6 12 months)"; "12_" -> "12+".
    """
    open_paren = False
    out: list[str] = []
    tokens = slug.strip("-").split("-")
    for index, token in enumerate(tokens):
        if token == "":
            out.append("&")
            continue
        last = index == len(tokens) - 1
        prefix = suffix = ""
        if token.startswith("_"):
            token, prefix, open_paren = token[1:], "(", True
        if token.endswith("_"):
            token = token[:-1]
            if open_paren and last:
                suffix, open_paren = ")", False
            else:
                suffix = "+"
        token = re.sub(r"(?<=[a-z])_(?=s(?:$))", "'", token)
        token = token.replace("cr_me", "crème").replace("fra_che", "fraîche")
        token = re.sub(r"(?<=[a-z])_(?=[a-z])", "e", token)
        out.append(prefix + token + suffix)
    words = []
    for i, word in enumerate(out):
        if word != "&" and (i == 0 or word not in _SMALL_WORDS):
            word = word[:1].upper() + word[1:] if not word.startswith("(") else "(" + word[1:2].upper() + word[2:]
        words.append(word)
    return re.sub(r"\s+", " ", " ".join(words)).strip()


def woolworths_slug(label: str) -> str:
    """Woolworths' URL form of an aisle label: "Oil, Vinegar & Condiments" -> "oil-vinegar-condiments"."""
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


# --- building ------------------------------------------------------------

def _with_parents(paths_and_names: dict[str, str]) -> list[Category]:
    """Categories for these paths, each pointing at its nearest listed ancestor."""
    out = []
    for path in sorted(paths_and_names):
        parts = path.split("/")
        parent = ""
        for cut in range(len(parts) - 1, 0, -1):
            candidate = "/".join(parts[:cut])
            if candidate in paths_and_names:
                parent = candidate
                break
        out.append(Category(path, paths_and_names[path], parent))
    return out


def sitemap_locs(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    return [(el.text or "").strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]


def category_sitemap_urls(index_xml: str) -> list[str]:
    return [loc for loc in sitemap_locs(index_xml) if "categor" in loc.rsplit("/", 1)[-1].lower()]


def build_foodstuffs_tree(urls: list[str], rules: RobotsRules) -> tuple[list[Category], int]:
    """Categories from sitemap page URLs. Returns (categories, number skipped by robots.txt)."""
    names: dict[str, str] = {}
    skipped = 0
    for url in urls:
        url_path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
        if not url_path.startswith(FOODSTUFFS_CATEGORY_PREFIX):
            continue
        path = url_path[len(FOODSTUFFS_CATEGORY_PREFIX):].strip("/")
        if not path or path in names:
            continue
        if not rules.is_allowed(FOODSTUFFS_CATEGORY_PREFIX + path):
            skipped += 1
            continue
        names[path] = slug_to_name(path.rsplit("/", 1)[-1])
    return _with_parents(names), skipped


def build_woolworths_tree(shell: dict, rules: RobotsRules) -> tuple[list[Category], int]:
    """Departments, aisles and shelves from the /api/v1/shell navigation."""
    departments = []
    for nav in shell.get("mainNavs") or []:
        if (nav.get("url") or "").rstrip("/").endswith("/browse/departments"):
            for group in nav.get("navigationItems") or []:
                departments.extend(group.get("items") or [])
    names: dict[str, str] = {}
    skipped = 0

    def add(path: str, name: str) -> None:
        nonlocal skipped
        if not path or not name or path in names:
            return
        if not rules.is_allowed(WOOLWORTHS_BROWSE_PREFIX + path):
            skipped += 1
            return
        names[path] = name

    for dept in departments:
        slug = (dept.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
        if not slug:
            continue
        add(slug, dept.get("label") or "")
        for aisle in dept.get("dasFacets") or []:
            aisle_name = aisle.get("name") or ""
            aisle_path = f"{slug}/{woolworths_slug(aisle_name)}" if aisle_name else ""
            add(aisle_path, aisle_name)
            for shelf in aisle.get("shelfResponses") or []:
                shelf_slug = shelf.get("url") or woolworths_slug(shelf.get("label") or "")
                if aisle_path and shelf_slug:
                    add(f"{aisle_path}/{shelf_slug}", shelf.get("label") or "")
    return _with_parents(names), skipped


# --- fetching ------------------------------------------------------------

def _site_root(store: str) -> str:
    return "https://" + SITES[store].store_cookie_domain


def _default_http_get(url: str) -> str:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT_SECONDS)
    if response.status_code != 200:
        raise ValueError(f"{url} responded with status {response.status_code}")
    return response.text


def _polite_sleep() -> None:
    time.sleep(PAGE_DELAY_SECONDS + random.uniform(0, PAGE_DELAY_JITTER_SECONDS))


def fetch_foodstuffs_tree(
    store: str, guard: RobotsGuard | None = None, http_get=_default_http_get, sleep=_polite_sleep
) -> list[Category]:
    """Pak'nSave / New World tree from the category sitemap. Raises on any failure."""
    root = _site_root(store)
    rules = (guard or RobotsGuard()).rules_for(root, SITES[store].supermarket)
    index_url = root + "/ecomsitemap_index.xml"
    if not rules.is_allowed(index_url):
        raise ValueError("robots.txt disallows the sitemap index")
    sitemaps = category_sitemap_urls(http_get(index_url))
    if not sitemaps:
        raise ValueError("the sitemap index lists no category sitemap")
    urls: list[str] = []
    for sitemap_url in sitemaps:
        if not rules.is_allowed(sitemap_url):
            log(f"  SKIPPED (robots.txt disallows it): {urllib.parse.urlsplit(sitemap_url).path}")
            continue
        sleep()
        urls.extend(sitemap_locs(http_get(sitemap_url)))
    categories, skipped = build_foodstuffs_tree(urls, rules)
    if skipped:
        log(f"  {skipped} category path(s) left out because robots.txt disallows them.")
    return categories


def fetch_woolworths_tree(guard: RobotsGuard | None = None) -> list[Category]:
    from scraper import woolworths_transport as transport

    shell = transport.get_navigation()
    rules = transport.ROBOTS_GUARD.rules_for(transport.SITE_ROOT, "Woolworths")
    categories, skipped = build_woolworths_tree(shell, rules)
    if skipped:
        log(f"  {skipped} category path(s) left out because robots.txt disallows them.")
    return categories


def fetch_tree(store: str, guard: RobotsGuard | None = None) -> list[Category]:
    """The store's current tree, or raises. An empty tree counts as a failure."""
    categories = fetch_woolworths_tree(guard) if store == "woolworths" else fetch_foodstuffs_tree(store, guard)
    if not categories:
        raise ValueError("no categories found")
    return categories


# --- cache ---------------------------------------------------------------

def cache_path(store: str) -> Path:
    return Path(STATE_DIR) / "category_trees" / f"{store}.json"


def load_cache(store: str) -> dict | None:
    try:
        data = json.loads(cache_path(store).read_text())
        data["categories"] = categories_from_json(data["categories"])
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_cache(store: str, categories: list[Category], updated_at: str, posted_hash: str | None) -> None:
    path = cache_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "store": store,
        "updatedAt": updated_at,
        "hash": tree_hash(categories),
        "postedHash": posted_hash,
        "categories": [c.to_json() for c in sorted(categories, key=lambda c: c.path)],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=1))
    tmp.replace(path)


# --- posting -------------------------------------------------------------

def post_url(mailbox_url: str) -> str:
    parts = urllib.parse.urlsplit(mailbox_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"{MAILBOX_URL_ENV} is not a full URL")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, POST_PATH, "", ""))


def post_tree(store: str, categories: list[Category], updated_at: str) -> tuple[bool, str]:
    """POST the tree to the app. Returns (ok, short token-free reason). Never raises."""
    mailbox_url = os.environ.get(MAILBOX_URL_ENV)
    token = os.environ.get(MAILBOX_TOKEN_ENV)
    if not mailbox_url or not token:
        return False, f"{MAILBOX_URL_ENV}/{MAILBOX_TOKEN_ENV} not set"
    try:
        url = post_url(mailbox_url)
    except ValueError as e:
        return False, str(e)
    body = {
        "store": store,
        "updatedAt": updated_at,
        "categories": [c.to_json() for c in sorted(categories, key=lambda c: c.path)],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": bearer(token),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_SECONDS) as response:
            status = response.status
        if 200 <= status < 300:
            return True, f"status {status}"
        return False, f"status {status}"
    except urllib.error.HTTPError as e:
        return False, f"status {e.code} ({e.reason})"
    except urllib.error.URLError as e:
        return False, f"could not connect ({e.reason})"
    except TimeoutError:
        return False, f"timed out after {POST_TIMEOUT_SECONDS}s"
    except Exception as e:
        return False, f"unexpected error ({type(e).__name__})"


# --- one run's worth -----------------------------------------------------

@dataclass
class TreeSync:
    categories: list[Category]
    source: str  # "site", "cache" or "none"
    changed: bool = False
    posted: bool = False


def sync_tree(store: str, *, save: bool, guard: RobotsGuard | None = None, fetch=None) -> TreeSync:
    """Get the store's tree, and (when save) cache it and post it if it changed.

    Never raises. If the site can't be read the cached tree is used for mapping and
    nothing is posted. save=False reads and reports but writes and posts nothing.
    """
    fetch = fetch or fetch_tree
    cached = load_cache(store)
    try:
        categories = fetch(store, guard)
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        if cached:
            log(f"Category tree ({store}): could not read the site ({reason}) - using the cached tree, nothing posted.")
            return TreeSync(cached["categories"], "cache")
        log(f"Category tree ({store}): could not read the site ({reason}) and there is no cached tree.")
        return TreeSync([], "none")

    digest = tree_hash(categories)
    changed = not cached or cached.get("postedHash") != digest
    label = "changed since the last post" if changed else "unchanged since the last post"
    if not save:
        log(f"Category tree ({store}): {len(categories)} categories, {label}. Dry run - nothing posted or cached.")
        return TreeSync(categories, "site", changed)

    updated_at = (
        cached["updatedAt"] if cached and cached.get("hash") == digest and cached.get("updatedAt")
        else datetime.now(timezone.utc).isoformat()
    )
    posted_hash = cached.get("postedHash") if cached else None
    posted = False
    if changed:
        ok, reason = post_tree(store, categories, updated_at)
        if ok:
            posted, posted_hash = True, digest
            log(f"Category tree ({store}): {len(categories)} categories, {label} - posted.")
        else:
            log(f"Category tree ({store}): {len(categories)} categories, {label} - post failed ({reason}); will retry next run.")
    else:
        log(f"Category tree ({store}): {len(categories)} categories, {label} - not posted.")
    try:
        save_cache(store, categories, updated_at, posted_hash)
    except OSError as e:
        log(f"Category tree ({store}): could not cache it ({type(e).__name__}).")
    return TreeSync(categories, "site", changed, posted)
