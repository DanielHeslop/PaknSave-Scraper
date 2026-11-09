"""The Foodstuffs category files vs the app's term lists.

Every watch term and each store's top-30 search terms must either map to a
category that is really in that store's file (and whose label/covers words
include the term), or be listed as having no category. Every category URL
must be on the right site and allowed by that site's robots.txt (saved in
tests/fixtures/ on the day the paths were checked).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from scraper.browser import SITES, parse_categories_file
from scraper.robots import RobotsRules
from scraper.search_step import category_words, term_has_scraped_home

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
HOSTS = {"paknsave": "www.paknsave.co.nz", "newworld": "www.newworld.co.nz"}
MAPPING = json.loads((FIXTURES / "term_categories.json").read_text())


def _pages(store: str):
    return parse_categories_file(ROOT / SITES[store].default_categories_file)


def _covers_by_label(store: str) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for page in _pages(store):
        out.setdefault(page.category, page.covers)
    return out


def _terms(store: str) -> list[str]:
    return list(dict.fromkeys(MAPPING["watch_terms"] + MAPPING["top30"][store]))


@pytest.mark.parametrize("store", sorted(HOSTS))
def test_category_urls_are_on_the_site_and_allowed_by_robots(store):
    rules = RobotsRules.parse((FIXTURES / f"robots_{store}.txt").read_text())
    for page in _pages(store):
        assert page.url.startswith(f"https://{HOSTS[store]}/shop/category/"), page.url
        assert rules.is_allowed(page.url), f"robots.txt disallows {page.url}"


@pytest.mark.parametrize("store", sorted(HOSTS))
def test_each_label_names_one_page(store):
    urls = defaultdict(set)
    for page in _pages(store):
        urls[page.category].add(page.url.split("?")[0])
    clashes = {label: sorted(u) for label, u in urls.items() if len(u) > 1}
    assert not clashes


def test_both_stores_have_the_same_categories():
    assert set(_covers_by_label("paknsave")) == set(_covers_by_label("newworld"))


@pytest.mark.parametrize("store", sorted(HOSTS))
def test_every_watch_and_top_term_is_mapped_or_listed_as_having_no_category(store):
    mapped = MAPPING["category_for_term"]
    unmapped = MAPPING["no_category"]
    missing = [t for t in _terms(store) if t not in mapped and t not in unmapped]
    assert not missing, f"map these to a category or list them under no_category: {missing}"


@pytest.mark.parametrize("store", sorted(HOSTS))
def test_mapped_terms_point_at_a_scraped_category_that_covers_them(store):
    covers = _covers_by_label(store)
    for term in _terms(store):
        label = MAPPING["category_for_term"].get(term)
        if label is None:
            continue
        assert label in covers, f"{term!r} -> {label!r}, which {store} does not scrape"
        words = category_words(label, covers[label])
        assert term_has_scraped_home(term, words), f"{term!r} shares no word with {label!r}"


def test_terms_with_no_category_are_not_also_mapped():
    assert not set(MAPPING["no_category"]) & set(MAPPING["category_for_term"])
