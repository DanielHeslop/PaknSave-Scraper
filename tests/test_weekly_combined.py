"""Tests for scraper/weekly_combined.py's three steps (scrape, search, publish)
and the --publish-snapshot recovery path.

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

import scraper.category_mapping as category_mapping
import scraper.price_runs as price_runs
import scraper.weekly_combined as weekly_combined
from scraper.ingredients import StoreLists
from scraper.price_runs import RunResult
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
            "categories_scraped": 1,
            "products_found": 1,
            "products_saved": 1,
            "products_with_unit_price": 1,
            "products_without_unit_price": 0,
            "products_dropped": [],
        },
        "scraped_categories": {"vegetables": ("carrot", "onion")},
    }


def _base_args(tmp_path, **overrides):
    args = parse_args(["--save"])
    args.snapshots_dir = tmp_path / "snapshots"
    args.history_path = tmp_path / "price_history.json"
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _lists(search=("carrots", "saffron", "dry white wine"), watch=("onion",), skip=()):
    return StoreLists(
        store="paknsave",
        search_terms=list(search),
        watch_terms=list(watch),
        skip=frozenset(skip),
    )


def _patch(monkeypatch, lists, published, reports):
    monkeypatch.setattr(weekly_combined, "scrape_all", _fake_scrape_all)
    monkeypatch.setattr(weekly_combined, "fetch_store_lists", lambda store, url: lists)
    monkeypatch.setattr(
        weekly_combined,
        "publish_run",
        lambda products, *a, **kw: published.append(products) or RunResult(items=len(products)),
    )
    monkeypatch.setattr(
        weekly_combined, "report_outcome", lambda store, outcome: reports.append((store, outcome))
    )


def test_search_step_matches_locally_and_reports_found_and_not_found(monkeypatch, tmp_path):
    published, reports = [], []
    _patch(monkeypatch, _lists(), published, reports)

    assert asyncio.run(main_async(_base_args(tmp_path))) == 0

    (store, outcome), = reports
    assert store == "paknsave"
    assert set(outcome.found) == {"carrots"}
    # onion belongs to the scraped vegetables category and nothing matched -> not found.
    assert outcome.not_found == ["onion"]
    # saffron / dry white wine have no scraped category to blame -> left unreported.
    assert set(outcome.unreported) == {"saffron", "dry white wine"}
    assert published == [_CATEGORY_PRODUCTS]


def test_search_step_failure_still_publishes(monkeypatch, tmp_path, capsys):
    published, reports = [], []
    _patch(monkeypatch, _lists(), published, reports)
    monkeypatch.setattr(
        weekly_combined, "run_search_step", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    assert asyncio.run(main_async(_base_args(tmp_path))) == 0

    assert published == [_CATEGORY_PRODUCTS]
    assert "Search step failed" in capsys.readouterr().out


def test_unreachable_app_list_skips_search_but_publishes(monkeypatch, tmp_path):
    published, reports = [], []
    _patch(monkeypatch, None, published, reports)

    assert asyncio.run(main_async(_base_args(tmp_path))) == 0

    assert reports == []
    assert published == [_CATEGORY_PRODUCTS]


def test_dry_run_publishes_and_reports_nothing(monkeypatch, tmp_path, capsys):
    published, reports = [], []
    _patch(monkeypatch, _lists(), published, reports)
    args = _base_args(tmp_path, save=False)

    assert asyncio.run(main_async(args)) == 0

    assert published == [] and reports == []
    out = capsys.readouterr().out
    assert "would report" in out and "SUMMARY:" in out


def test_summary_line_is_printed_last(monkeypatch, tmp_path, capsys):
    published, reports = [], []
    _patch(monkeypatch, _lists(skip=["saffron"]), published, reports)

    asyncio.run(main_async(_base_args(tmp_path)))

    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("SUMMARY: products published 1, terms searched 3, found 1, not found 1")
    assert "skipped 1" in last and "categories scraped 1" in last


def test_publish_snapshot_file_dry_run_reports_counts_without_posting(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "2026-10-03.json"
    snapshot_path.write_text(json.dumps(_CATEGORY_PRODUCTS))

    posted = []
    monkeypatch.setattr(
        price_runs,
        "publish_run",
        lambda products, store, scraped_at, dry_run=False, **kw: posted.append((products, dry_run)) or RunResult(),
    )

    monkeypatch.setattr(category_mapping, "label_paths", lambda store, categories_file=None: {})

    exit_code = publish_snapshot_file(str(snapshot_path), save=False)

    assert exit_code == 0
    assert posted == [(_CATEGORY_PRODUCTS, True)]


def test_publish_snapshot_file_save_actually_publishes(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "2026-10-03.json"
    snapshot_path.write_text(json.dumps(_CATEGORY_PRODUCTS))

    posted = []
    monkeypatch.setattr(
        price_runs,
        "publish_run",
        lambda products, store, scraped_at, dry_run=False, **kw: posted.append((products, dry_run)) or RunResult(),
    )

    monkeypatch.setattr(category_mapping, "label_paths", lambda store, categories_file=None: {})

    exit_code = publish_snapshot_file(str(snapshot_path), save=True)

    assert exit_code == 0
    assert posted == [(_CATEGORY_PRODUCTS, False)]


def test_publish_snapshot_file_fills_store_category_from_the_label(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "2026-10-03.json"
    snapshot_path.write_text(json.dumps(_CATEGORY_PRODUCTS))
    posted = []
    monkeypatch.setattr(
        price_runs,
        "publish_run",
        lambda products, store, scraped_at, dry_run=False, **kw: posted.append(products) or RunResult(),
    )
    monkeypatch.setattr(
        category_mapping, "label_paths", lambda store, categories_file=None: {"vegetables": "fruit-and-vegetables/vegetables"}
    )

    publish_snapshot_file(str(snapshot_path), save=False)

    assert posted[0][0]["store_category"] == "fruit-and-vegetables/vegetables"
    assert "store_category" not in _CATEGORY_PRODUCTS[0]


def test_publish_snapshot_file_missing_file_returns_error(tmp_path):
    exit_code = publish_snapshot_file(str(tmp_path / "does-not-exist.json"), save=False)
    assert exit_code == 1


def test_scrape_time_from_the_category_scrape_is_what_the_run_is_posted_with(monkeypatch, tmp_path):
    seen = []

    async def scrape_with_time(*args, **kwargs):
        result = await _fake_scrape_all()
        result["scraped_at"] = "2026-11-08T08:01:02+13:00"
        return result

    _patch(monkeypatch, _lists(), [], [])
    monkeypatch.setattr(weekly_combined, "scrape_all", scrape_with_time)
    monkeypatch.setattr(
        weekly_combined,
        "publish_run",
        lambda products, store, scraped_at, **kw: seen.append((store, scraped_at, kw.get("legacy"))) or RunResult(),
    )

    assert asyncio.run(main_async(_base_args(tmp_path))) == 0
    assert seen == [("paknsave", "2026-11-08T08:01:02+13:00", False)]


def test_legacy_mailbox_flag_reaches_publish_run(monkeypatch, tmp_path):
    seen = []
    _patch(monkeypatch, _lists(), [], [])
    monkeypatch.setattr(
        weekly_combined,
        "publish_run",
        lambda products, store, scraped_at, **kw: seen.append(kw.get("legacy")) or RunResult(),
    )
    args = _base_args(tmp_path, legacy_mailbox=True)
    assert asyncio.run(main_async(args)) == 0
    assert seen == [True]
