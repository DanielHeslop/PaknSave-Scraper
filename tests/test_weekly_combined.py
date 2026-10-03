"""Tests for scraper/weekly_combined.py's Part 1 / Part 2 resilience and the
--publish-snapshot recovery path.

Context: on 2026-09-27 and 2026-10-03, Part 2's ingredient-list fetch failed
with a 401 and the whole run exited before scraper.publish.publish_to_mailbox
was ever called - so Part 1's category-scrape results, already safely
written to data/snapshots/ and data/price_history.json, never reached the
mailbox. The fix publishes Part 1's results immediately after they're
written, before Part 2 runs at all, so a Part 2 failure can no longer cost
them.
"""

from __future__ import annotations

import asyncio
import json

import scraper.weekly_combined as weekly_combined
from scraper.ingredients import IngredientListError
from scraper.weekly_combined import main_async, parse_args, publish_snapshot_file

_CATEGORY_PRODUCTS = [
    {
        "product_id": "P1",
        "name": "Carrots",
        "category": "vegetables",
        "size": "kg",
        "price": 1.99,
        "unit_price": 1.99,
        "unit": "kg",
        "scraped_at": "2026-10-03",
        "supermarket": "Pak'nSave",
    }
]


async def _fake_scrape_all(*args, **kwargs):
    return {
        "products": _CATEGORY_PRODUCTS,
        "stats": {
            "pages_scraped": 1,
            "pages_skipped": [],
            "products_found": 1,
            "products_saved": 1,
            "products_with_unit_price": 1,
            "products_without_unit_price": 0,
            "products_dropped": [],
        },
    }


def _base_args(tmp_path, **overrides):
    argv = ["--save"]
    args = parse_args(argv)
    args.snapshots_dir = tmp_path / "snapshots"
    args.search_snapshots_dir = tmp_path / "search_snapshots"
    args.history_path = tmp_path / "price_history.json"
    args.cache_path = tmp_path / "search_cache.json"
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_part1_publishes_before_part2_runs_and_survives_part2_failure(monkeypatch, tmp_path):
    publish_calls = []

    monkeypatch.setattr(weekly_combined, "scrape_all", _fake_scrape_all)
    monkeypatch.setattr(
        weekly_combined,
        "fetch_ingredient_list",
        lambda url: (_ for _ in ()).throw(IngredientListError("boom: 401")),
    )
    monkeypatch.setattr(
        weekly_combined, "publish_to_mailbox", lambda products, **kw: publish_calls.append(products)
    )

    args = _base_args(tmp_path)
    exit_code = asyncio.run(main_async(args))

    assert exit_code == 1
    assert publish_calls == [_CATEGORY_PRODUCTS]


def test_part2_failure_is_logged_clearly(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(weekly_combined, "scrape_all", _fake_scrape_all)
    monkeypatch.setattr(
        weekly_combined,
        "fetch_ingredient_list",
        lambda url: (_ for _ in ()).throw(
            IngredientListError(f"{url} responded with status 401 (Unauthorized)")
        ),
    )
    monkeypatch.setattr(weekly_combined, "publish_to_mailbox", lambda products, **kw: None)

    args = _base_args(tmp_path)
    exit_code = asyncio.run(main_async(args))

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "Could not fetch the ingredient list - stopping run" in out
    assert "401" in out


def test_part1_not_published_twice_when_part2_succeeds_with_no_search_products(monkeypatch, tmp_path):
    """When Part 2 runs to completion but finds no NEW products to search
    for (its one ingredient is already covered by the category scrape), it
    must not re-publish category_products a second time - it was already
    published right after Part 1, and search_products is empty so Part 2's
    own `if search_products:` publish guard is skipped entirely."""
    publish_calls = []

    async def _fake_search_all(*args, **kwargs):
        return {
            "products": [],
            "stats": {
                "ingredients_total": 0,
                "ingredients_already_fresh": [],
                "ingredients_searched": 0,
                "ingredients_matched": 0,
                "ingredients_no_match": [],
            },
            "newly_found": [],
        }

    monkeypatch.setattr(weekly_combined, "scrape_all", _fake_scrape_all)
    monkeypatch.setattr(weekly_combined, "fetch_ingredient_list", lambda url: ["Carrots"])
    monkeypatch.setattr(weekly_combined, "search_all", _fake_search_all)
    monkeypatch.setattr(
        weekly_combined, "publish_to_mailbox", lambda products, **kw: publish_calls.append(products)
    )

    args = _base_args(tmp_path)
    exit_code = asyncio.run(main_async(args))

    assert exit_code == 0
    assert publish_calls == [_CATEGORY_PRODUCTS]


def test_publish_snapshot_file_dry_run_reports_counts_without_posting(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "2026-10-03.json"
    snapshot_path.write_text(json.dumps(_CATEGORY_PRODUCTS))

    posted = []
    monkeypatch.setattr(
        weekly_combined,
        "publish_to_mailbox",
        lambda products, dry_run=False: posted.append((products, dry_run)),
    )

    exit_code = publish_snapshot_file(str(snapshot_path), save=False)

    assert exit_code == 0
    assert posted == [(_CATEGORY_PRODUCTS, True)]


def test_publish_snapshot_file_save_actually_publishes(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "2026-10-03.json"
    snapshot_path.write_text(json.dumps(_CATEGORY_PRODUCTS))

    posted = []
    monkeypatch.setattr(
        weekly_combined,
        "publish_to_mailbox",
        lambda products, dry_run=False: posted.append((products, dry_run)),
    )

    exit_code = publish_snapshot_file(str(snapshot_path), save=True)

    assert exit_code == 0
    assert posted == [(_CATEGORY_PRODUCTS, False)]


def test_publish_snapshot_file_missing_file_returns_error(tmp_path):
    exit_code = publish_snapshot_file(str(tmp_path / "does-not-exist.json"), save=False)
    assert exit_code == 1
