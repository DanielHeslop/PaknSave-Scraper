"""Category trees: building each store's tree from saved samples, caching, and
posting with the app's endpoint mocked. No test touches the network."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

import scraper.category_tree as category_tree
from scraper.category_tree import (
    Category,
    build_foodstuffs_tree,
    build_woolworths_tree,
    category_sitemap_urls,
    fetch_foodstuffs_tree,
    post_url,
    sitemap_locs,
    slug_to_name,
    sync_tree,
    tree_hash,
)
from scraper.robots import RobotsGuard, RobotsRules

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "s3cret-token-value"
MAILBOX = "https://example-site.netlify.app/api/price-mailbox"


def _read(name):
    return (FIXTURES / name).read_text()


def _by_path(categories):
    return {c.path: c for c in categories}


@pytest.mark.parametrize(
    "slug, name",
    [
        ("pasta-rice--noodles", "Pasta Rice & Noodles"),
        ("baby-food-12_-months", "Baby Food 12+ Months"),
        ("follow-on-_6-12-months_", "Follow On (6 12 Months)"),
        ("men_s-health", "Men's Health"),
        ("cr_me-fra_che", "Crème Fraîche"),
        ("baby-and-toddler", "Baby and Toddler"),
    ],
)
def test_slug_to_name(slug, name):
    assert slug_to_name(slug) == name


@pytest.mark.parametrize("store", ["paknsave", "newworld"])
def test_foodstuffs_tree_is_built_from_the_category_sitemap(store):
    urls = sitemap_locs(_read(f"sitemap_categories_{store}.xml"))
    categories, skipped = build_foodstuffs_tree(urls, RobotsRules())
    tree = _by_path(categories)
    assert skipped == 0
    assert len(categories) == len(urls) == len(tree)

    top = tree["pantry"]
    assert top == Category("pantry", "Pantry", "")
    sub = tree["pantry/baking-supplies--sugar"]
    assert sub.name == "Baking Supplies & Sugar" and sub.parent == "pantry"
    leaf = tree["pantry/baking-supplies--sugar/flour"]
    assert leaf.parent == "pantry/baking-supplies--sugar"
    # the ?pg=1 on the sitemap's URLs never reaches a path
    assert all("?" not in c.path for c in categories)


def test_foodstuffs_tree_drops_paths_robots_disallows():
    urls = sitemap_locs(_read("sitemap_categories_paknsave.xml"))
    rules = RobotsRules.parse("User-agent: *\nDisallow: /shop/category/featured\n")
    categories, skipped = build_foodstuffs_tree(urls, rules)
    assert skipped == 3
    assert not any(c.path.startswith("featured") for c in categories)
    assert "pantry" in _by_path(categories)


def test_parent_is_the_nearest_listed_ancestor():
    urls = ["https://www.paknsave.co.nz/shop/category/a/b/c?pg=1", "https://www.paknsave.co.nz/shop/category/a?pg=1"]
    tree = _by_path(build_foodstuffs_tree(urls, RobotsRules())[0])
    assert tree["a/b/c"].parent == "a"


def test_category_sitemap_is_picked_from_the_index():
    assert category_sitemap_urls(_read("sitemap_index_newworld.xml")) == [
        "https://www.newworld.co.nz/ecom_sitemap_categories.xml"
    ]


def test_woolworths_tree_has_departments_aisles_and_shelves():
    shell = json.loads(_read("woolworths_shell.json"))
    categories, skipped = build_woolworths_tree(shell, RobotsRules())
    tree = _by_path(categories)
    assert skipped == 0
    assert tree["pantry"] == Category("pantry", "Pantry", "")
    assert tree["pantry/baking"] == Category("pantry/baking", "Baking", "pantry")
    assert tree["pantry/oil-vinegar-condiments"].name == "Oil, Vinegar & Condiments"
    shelf = tree["pantry/oil-vinegar-condiments/oil"]
    assert shelf.parent == "pantry/oil-vinegar-condiments"
    assert tree["household/bathroom/toilet-bathroom-cleaners"].name == "Toilet & Bathroom Cleaners"
    # links outside the Department group (home, recipes) are not categories
    assert not any(c.path in ("", "recipes") for c in categories)


def test_woolworths_tree_drops_paths_robots_disallows():
    shell = json.loads(_read("woolworths_shell.json"))
    rules = RobotsRules.parse("User-agent: *\nDisallow: /shop/browse/household\n")
    categories, skipped = build_woolworths_tree(shell, rules)
    assert skipped > 0
    assert not any(c.path.startswith("household") for c in categories)
    assert "pantry/baking" in _by_path(categories)


def test_fetch_reads_only_the_category_sitemap_and_pauses_between_requests():
    requested, pauses = [], []
    pages = {
        "https://www.paknsave.co.nz/ecomsitemap_index.xml": _read("sitemap_index_paknsave.xml"),
        "https://www.paknsave.co.nz/ecom_sitemap_categories.xml": _read("sitemap_categories_paknsave.xml"),
    }

    def http_get(url):
        requested.append(url)
        return pages[url]

    categories = fetch_foodstuffs_tree(
        "paknsave", RobotsGuard(lambda root, label: RobotsRules()), http_get, lambda: pauses.append(1)
    )
    assert requested == list(pages)  # never a product sitemap
    assert len(pauses) == 1
    assert "pantry" in _by_path(categories)


def test_fetch_refuses_a_sitemap_robots_disallows():
    rules = RobotsRules.parse("User-agent: *\nDisallow: /ecom_sitemap_categories.xml\n")
    requested = []

    def http_get(url):
        requested.append(url)
        return _read("sitemap_index_paknsave.xml")

    categories = fetch_foodstuffs_tree("paknsave", RobotsGuard(lambda r, l: rules), http_get, lambda: None)
    assert categories == []  # fetch_tree treats an empty tree as a failure
    assert requested == ["https://www.paknsave.co.nz/ecomsitemap_index.xml"]


# --- posting -------------------------------------------------------------

class _Response:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Endpoint:
    """Stands in for urllib.request.urlopen against the app."""

    def __init__(self, monkeypatch, status=200, error=None):
        self.requests = []
        self.status, self.error = status, error
        monkeypatch.setattr("urllib.request.urlopen", self)

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return _Response(self.status)

    def bodies(self):
        return [json.loads(r.data) for r in self.requests]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("MAILBOX_URL", MAILBOX)
    monkeypatch.setenv("MAILBOX_TOKEN", TOKEN)


def _tree(*paths):
    return [Category(p, p.rsplit("/", 1)[-1].title(), p.rsplit("/", 1)[0] if "/" in p else "") for p in paths]


def test_post_url_is_the_origin_of_the_mailbox_url():
    assert post_url(MAILBOX) == "https://example-site.netlify.app/api/store-categories"


def test_changed_tree_is_posted_once_then_unchanged_is_not(monkeypatch, env, capsys):
    endpoint = _Endpoint(monkeypatch)
    tree = _tree("pantry", "pantry/baking")

    first = sync_tree("paknsave", save=True, fetch=lambda s, g: tree)
    assert (first.changed, first.posted, first.source) == (True, True, "site")
    (request,) = endpoint.requests
    assert request.full_url == "https://example-site.netlify.app/api/store-categories"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    (body,) = endpoint.bodies()
    assert body["store"] == "paknsave"
    assert body["updatedAt"]
    assert body["categories"] == [
        {"path": "pantry", "name": "Pantry", "parent": ""},
        {"path": "pantry/baking", "name": "Baking", "parent": "pantry"},
    ]

    second = sync_tree("paknsave", save=True, fetch=lambda s, g: tree)
    assert (second.changed, second.posted) == (False, False)
    assert len(endpoint.requests) == 1

    out = capsys.readouterr().out
    assert "posted" in out and "not posted" in out
    assert TOKEN not in out


def test_updated_at_stays_the_same_while_the_tree_does(monkeypatch, env):
    _Endpoint(monkeypatch)
    tree = _tree("pantry")
    sync_tree("paknsave", save=True, fetch=lambda s, g: tree)
    stamp = category_tree.load_cache("paknsave")["updatedAt"]
    sync_tree("paknsave", save=True, fetch=lambda s, g: tree)
    assert category_tree.load_cache("paknsave")["updatedAt"] == stamp


def test_a_changed_tree_is_posted_again(monkeypatch, env):
    endpoint = _Endpoint(monkeypatch)
    sync_tree("newworld", save=True, fetch=lambda s, g: _tree("pantry"))
    result = sync_tree("newworld", save=True, fetch=lambda s, g: _tree("pantry", "bakery"))
    assert result.posted
    assert [len(b["categories"]) for b in endpoint.bodies()] == [1, 2]


def test_each_store_has_its_own_cache(monkeypatch, env):
    endpoint = _Endpoint(monkeypatch)
    tree = _tree("pantry")
    sync_tree("paknsave", save=True, fetch=lambda s, g: tree)
    result = sync_tree("newworld", save=True, fetch=lambda s, g: tree)
    assert result.posted
    assert [b["store"] for b in endpoint.bodies()] == ["paknsave", "newworld"]


def test_failed_post_is_retried_next_run_and_never_prints_the_token(monkeypatch, env, capsys):
    endpoint = _Endpoint(monkeypatch, error=urllib.error.HTTPError(MAILBOX, 401, "Unauthorized", {}, io.BytesIO()))
    tree = _tree("pantry")
    result = sync_tree("paknsave", save=True, fetch=lambda s, g: tree)
    assert (result.changed, result.posted) == (True, False)
    assert "post failed" in capsys.readouterr().out

    endpoint.error = None
    retry = sync_tree("paknsave", save=True, fetch=lambda s, g: tree)
    assert retry.posted
    assert len(endpoint.requests) == 2
    assert TOKEN not in capsys.readouterr().out


def test_dry_run_posts_and_caches_nothing(monkeypatch, env):
    endpoint = _Endpoint(monkeypatch)
    result = sync_tree("paknsave", save=False, fetch=lambda s, g: _tree("pantry"))
    assert result.categories and not result.posted
    assert endpoint.requests == []
    assert category_tree.load_cache("paknsave") is None


def test_site_failure_uses_the_cached_tree_and_posts_nothing(monkeypatch, env, capsys):
    endpoint = _Endpoint(monkeypatch)
    tree = _tree("pantry", "pantry/baking")
    sync_tree("paknsave", save=True, fetch=lambda s, g: tree)

    def broken(store, guard):
        raise RuntimeError("site down")

    result = sync_tree("paknsave", save=True, fetch=broken)
    assert result.source == "cache"
    assert [c.path for c in result.categories] == ["pantry", "pantry/baking"]
    assert len(endpoint.requests) == 1
    assert "using the cached tree" in capsys.readouterr().out


def test_site_failure_with_no_cache_gives_an_empty_tree(monkeypatch, env):
    def broken(store, guard):
        raise RuntimeError("site down")

    result = sync_tree("paknsave", save=True, fetch=broken)
    assert result.source == "none" and result.categories == []


def test_missing_mailbox_settings_skip_the_post_quietly(monkeypatch):
    monkeypatch.delenv("MAILBOX_URL", raising=False)
    monkeypatch.delenv("MAILBOX_TOKEN", raising=False)
    result = sync_tree("paknsave", save=True, fetch=lambda s, g: _tree("pantry"))
    assert result.changed and not result.posted
    assert category_tree.load_cache("paknsave")["postedHash"] is None


def test_tree_hash_ignores_order_and_notices_changes():
    a, b = _tree("a", "b"), _tree("b", "a")
    assert tree_hash(a) == tree_hash(b)
    assert tree_hash(a) != tree_hash(_tree("a", "c"))
