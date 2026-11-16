"""Categories cut short by their page limit are read on to the end, and remembered.

A category is cut short when its last page at the page limit was full and reading on found more.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import scraper.weekly_combined as weekly_combined
from scraper import page_limits
from scraper.page_limits import PageStat
from test_watch_pagination import BASE, FULL, FakeSite, _ids, _scrape
from test_weekly_combined import _base_args, _fake_scrape_all, _lists, _patch

SPICES = BASE + "pantry/spices-seasoning--coatings"
SPICES_PATH = "pantry/spices-seasoning--coatings"


def _pages(url: str, sizes: list[int]) -> dict[str, list[str]]:
    pages = {}
    for n, size in enumerate(sizes, 1):
        pages[url if n == 1 else f"{url}?pg={n}"] = _ids(f"p{n}-", size)
    return pages


def _spices(monkeypatch, tmp_path, sizes, pages_setting=3):
    site = FakeSite(_pages(SPICES, sizes))
    result = _scrape(monkeypatch, tmp_path, site, categories=f"{SPICES} pages={pages_setting} category=spices\n")
    return site, result, result["page_stats"][0]


# --- detection ------------------------------------------------------------------

def test_a_full_last_page_at_the_limit_is_flagged_and_is_cut_short_once_more_is_read():
    stat = PageStat("spices", "p", configured=3, limit=3)
    for n in (1, 2, 3):
        page_limits.note_page(stat, n, FULL, FULL, True)
    assert stat.full_at_limit and stat.pages_read == 3 and stat.last_page_full
    assert not stat.cut_short  # nothing found past the limit yet
    page_limits.note_page(stat, 4, 7, 7, False)
    assert stat.cut_short and stat.extra_pages == 1


def test_a_short_last_page_at_the_limit_is_not_cut_short():
    stat = PageStat("spices", "p", configured=3, limit=3)
    page_limits.note_page(stat, 1, FULL, FULL, True)
    page_limits.note_page(stat, 2, FULL, FULL, True)
    page_limits.note_page(stat, 3, 12, 12, False)
    assert not stat.full_at_limit and not stat.cut_short
    assert stat.last_page_items == 12 and not stat.last_page_full


def test_a_full_page_before_the_limit_is_not_cut_short():
    stat = PageStat("spices", "p", configured=3, limit=3)
    page_limits.note_page(stat, 2, FULL, FULL, True)
    assert not stat.full_at_limit and not stat.cut_short


def test_a_run_records_pages_read_last_page_items_and_fullness(monkeypatch, tmp_path):
    site, result, stat = _spices(monkeypatch, tmp_path, [FULL, FULL, 12])
    assert site.loaded == [SPICES, SPICES + "?pg=2", SPICES + "?pg=3"]
    assert (stat.label, stat.path) == ("spices", SPICES_PATH)
    assert (stat.pages_read, stat.last_page_items, stat.last_page_full) == (3, 12, False)
    assert not stat.cut_short and stat.ended == page_limits.SHORT_PAGE
    assert result["stats"]["categories_cut_short"] == 0 and result["stats"]["extra_pages_read"] == 0


# --- reading on to the end ------------------------------------------------------

def test_a_cut_short_category_is_read_on_until_a_short_page(monkeypatch, tmp_path, capsys):
    site, result, stat = _spices(monkeypatch, tmp_path, [FULL] * 6 + [3])
    assert len(site.loaded) == 7
    assert stat.cut_short and stat.pages_read == 7 and stat.items == 6 * FULL + 3
    assert stat.limit == 3 and stat.extra_pages == 4
    assert len(result["products"]) == 6 * FULL + 3
    assert result["stats"]["categories_cut_short"] == 1 and result["stats"]["extra_pages_read"] == 4
    assert site.pauses == 6  # the existing pause before every page after the first
    assert f"spices: limit 3 reached, read 7 pages, {6 * FULL + 3} items" in capsys.readouterr().out


def test_a_category_that_ends_exactly_at_the_limit_with_a_short_page_logs_nothing(monkeypatch, tmp_path, capsys):
    _spices(monkeypatch, tmp_path, [FULL, FULL, 9])
    assert "reached, read" not in capsys.readouterr().out


def test_reading_on_ends_at_an_empty_page(monkeypatch, tmp_path):
    site, _, stat = _spices(monkeypatch, tmp_path, [FULL] * 4, pages_setting=2)
    assert len(site.loaded) == 5  # the fifth page came back empty
    assert stat.pages_read == 4 and stat.cut_short and stat.ended == page_limits.EMPTY_PAGE


def test_reading_on_is_capped_at_twenty_pages(monkeypatch, tmp_path, capsys):
    site, _, stat = _spices(monkeypatch, tmp_path, [FULL] * 30)
    assert len(site.loaded) == page_limits.SAFETY_CAP_PAGES == 20
    assert stat.pages_read == 20 and stat.cut_short and stat.ended == page_limits.SAFETY_CAP
    assert "safety cap" in capsys.readouterr().out


def test_a_page_past_the_limit_that_fails_to_load_is_the_end_not_a_skipped_page(monkeypatch, tmp_path):
    site = FakeSite(_pages(SPICES, [FULL, FULL]))
    site.broken.add(SPICES + "?pg=3")
    result = _scrape(monkeypatch, tmp_path, site, categories=f"{SPICES} pages=2 category=spices\n")
    assert result["stats"]["pages_skipped"] == []
    stat = result["page_stats"][0]
    assert stat.full_at_limit and not stat.cut_short  # nothing was found past the limit


def test_mapped_watch_categories_are_tracked_too(monkeypatch, tmp_path):
    from test_watch_pagination import _mapped

    site = FakeSite(_pages(BASE + "pantry/bulk-foods", [FULL] * 7 + [5]))
    result = _scrape(monkeypatch, tmp_path, site, extra=[_mapped()])
    stat, = result["page_stats"]
    assert stat.label == "bulk-foods" and stat.limit == 5 and stat.pages_read == 8 and stat.cut_short


# --- remembered page counts -----------------------------------------------------

def test_a_cut_short_run_remembers_its_page_count_and_the_next_run_reads_that_many(monkeypatch, tmp_path, capsys):
    _, _, stat = _spices(monkeypatch, tmp_path, [FULL] * 5 + [3])
    assert stat.cut_short
    assert page_limits.remember_page_counts("paknsave", [stat]) == 1
    assert page_limits.load_page_counts("paknsave") == {SPICES_PATH: 6}

    capsys.readouterr()
    site, result, again = _spices(monkeypatch, tmp_path, [FULL] * 5 + [3])
    assert again.limit == 6 and again.configured == 3
    assert len(site.loaded) == 6  # no probe past the last page
    assert not again.cut_short and again.extra_pages == 0
    assert result["stats"]["categories_cut_short"] == 0
    assert "reached, read" not in capsys.readouterr().out


def test_the_remembered_count_never_lowers_the_limit_below_the_file(monkeypatch, tmp_path):
    page_limits.remember_page_counts("paknsave", [PageStat("spices", SPICES_PATH, 1, 1, pages_read=2)])
    _, _, stat = _spices(monkeypatch, tmp_path, [FULL, FULL, FULL, FULL], pages_setting=3)
    assert stat.limit == 3


def test_a_remembered_category_that_grows_is_cut_short_again_and_the_count_goes_up(monkeypatch, tmp_path):
    page_limits.remember_page_counts("paknsave", [PageStat("spices", SPICES_PATH, 3, 3, pages_read=4)])
    _, _, stat = _spices(monkeypatch, tmp_path, [FULL] * 6 + [1])
    assert stat.limit == 4 and stat.cut_short and stat.pages_read == 7
    page_limits.remember_page_counts("paknsave", [stat])
    assert page_limits.load_page_counts("paknsave") == {SPICES_PATH: 7}


def test_a_category_that_shrinks_is_forgotten_once_it_fits_the_file_again():
    page_limits.remember_page_counts("paknsave", [PageStat("spices", "x", 3, 3, pages_read=6, ended=page_limits.SHORT_PAGE)])
    assert page_limits.load_page_counts("paknsave") == {"x": 6}
    page_limits.remember_page_counts("paknsave", [PageStat("spices", "x", 3, 6, pages_read=3, ended=page_limits.SHORT_PAGE)])
    assert page_limits.load_page_counts("paknsave") == {}


@pytest.mark.parametrize("ended", [page_limits.LOAD_FAILED, page_limits.ROBOTS, page_limits.SAFETY_CAP])
def test_an_unsure_run_never_lowers_a_remembered_count(ended):
    page_limits.remember_page_counts("paknsave", [PageStat("s", "x", 3, 3, pages_read=6, ended=page_limits.SHORT_PAGE)])
    page_limits.remember_page_counts("paknsave", [PageStat("s", "x", 3, 6, pages_read=2, ended=ended)])
    assert page_limits.load_page_counts("paknsave") == {"x": 6}


def test_remembered_counts_are_kept_per_store():
    page_limits.remember_page_counts("paknsave", [PageStat("s", "x", 3, 3, pages_read=6, ended=page_limits.SHORT_PAGE)])
    assert page_limits.load_page_counts("newworld") == {}


def test_a_damaged_counts_file_is_ignored():
    path = page_limits.counts_path("paknsave")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert page_limits.load_page_counts("paknsave") == {}


# --- the summary, the per-run record and dry runs ----------------------------------

def test_summary_line_reports_cut_short_categories_and_extra_pages():
    from scraper.search_step import SearchOutcome, summary_line

    line = summary_line(5, SearchOutcome(), 3, 61, paging={"cut_short": 2, "extra_pages": 9})
    assert "categories scraped 3, categories cut short 2, extra pages read 9, matcher" in line
    assert "cut short" not in summary_line(5, SearchOutcome(), 3, 61)


def _scrape_with_page_stats(*args, **kwargs):
    async def run():
        result = await _fake_scrape_all(*args, **kwargs)
        stat = PageStat("spices", SPICES_PATH, 3, 3, pages_read=6, last_page_items=3, items=253, full_at_limit=True,
                        ended=page_limits.SHORT_PAGE)
        result["page_stats"] = [stat]
        return result

    return run()


def test_a_real_run_writes_the_record_and_remembers_the_counts_and_the_summary_reports_them(monkeypatch, tmp_path, capsys):
    _patch(monkeypatch, _lists(), [], [])
    monkeypatch.setattr(weekly_combined, "scrape_all", _scrape_with_page_stats)
    assert asyncio.run(weekly_combined.main_async(_base_args(tmp_path))) == 0

    assert page_limits.load_page_counts("paknsave") == {SPICES_PATH: 6}
    record = json.loads(page_limits.stats_path("paknsave").read_text())
    assert record["categories"][0]["label"] == "spices" and record["categories"][0]["extra_pages"] == 3
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert "categories cut short 1, extra pages read 3" in last


def test_a_dry_run_writes_no_page_state(monkeypatch, tmp_path):
    _patch(monkeypatch, _lists(), [], [])
    monkeypatch.setattr(weekly_combined, "scrape_all", _scrape_with_page_stats)
    assert asyncio.run(weekly_combined.main_async(_base_args(tmp_path, save=False))) == 0
    assert not (page_limits.counts_path("paknsave").parent).exists()
    assert not page_limits.stats_path("paknsave").parent.exists()
    assert not any((tmp_path / "state").glob("page_*"))


# --- Woolworths ---------------------------------------------------------------

def _woolworths_reply(monkeypatch, total: int, calls: list):
    from test_watch_pagination import _woolworths_reply as reply

    reply(monkeypatch, total, calls)


def test_woolworths_cut_short_is_a_full_last_page_at_the_limit_with_more_to_read(monkeypatch, capsys):
    import scraper.woolworths as woolworths

    calls: list[int] = []
    _woolworths_reply(monkeypatch, 700, calls)  # 5 full pages + 100
    result = woolworths.browse_all([woolworths.BrowsePage("pantry/oils", "oils", (), 5)])
    stat, = result["page_stats"]
    assert calls == [1, 2, 3, 4, 5, 6] and stat.cut_short and stat.pages_read == 6 and stat.items == 700
    assert not stat.last_page_full and stat.last_page_items == 100
    assert "oils: limit 5 reached, read 6 pages, 700 items" in capsys.readouterr().out
    assert (result["stats"]["categories_cut_short"], result["stats"]["extra_pages_read"]) == (1, 1)


def test_woolworths_a_category_that_ends_on_the_limit_is_not_cut_short(monkeypatch):
    import scraper.woolworths as woolworths

    calls: list[int] = []
    _woolworths_reply(monkeypatch, 600, calls)  # exactly 5 full pages: the total says that is all
    result = woolworths.browse_all([woolworths.BrowsePage("pantry/oils", "oils", (), 5)])
    stat, = result["page_stats"]
    assert calls == [1, 2, 3, 4, 5] and stat.last_page_full and stat.ended == page_limits.ALL_READ
    assert stat.full_at_limit and not stat.cut_short and result["stats"]["categories_cut_short"] == 0  # the total says that is all
    assert result["stats"]["extra_pages_read"] == 0


def test_woolworths_reading_on_is_capped_at_twenty_pages(monkeypatch):
    import scraper.woolworths as woolworths

    calls: list[int] = []
    _woolworths_reply(monkeypatch, 120 * 30, calls)
    result = woolworths.browse_all([woolworths.BrowsePage("pantry/oils", "oils", (), 5)])
    assert calls == list(range(1, 21))
    assert result["page_stats"][0].ended == page_limits.SAFETY_CAP


def test_woolworths_uses_the_remembered_page_count(monkeypatch):
    import scraper.woolworths as woolworths

    page_limits.remember_page_counts("woolworths", [PageStat("oils", "pantry/oils", 5, 5, pages_read=8, ended=page_limits.ALL_READ)])
    calls: list[int] = []
    _woolworths_reply(monkeypatch, 120 * 7 + 40, calls)
    result = woolworths.browse_all([woolworths.BrowsePage("pantry/oils", "oils", (), 5)])
    stat, = result["page_stats"]
    assert stat.limit == 8 and calls == list(range(1, 9)) and not stat.cut_short


def test_the_dry_run_check_lists_full_last_pages_and_writes_nothing(tmp_path, monkeypatch):
    from scraper import category_tree, page_limits_check

    monkeypatch.setattr(category_tree, "STATE_DIR", str(tmp_path))
    snap = tmp_path / "snapshots"
    snap.mkdir()
    products = [{"category": "spices"}] * 150 + [{"category": "herbs"}] * 20 + [{"category": "milk"}] * 50
    (snap / "2026-11-15.json").write_text(json.dumps(products))
    cats = tmp_path / "cats.txt"
    cats.write_text(
        f"{SPICES} pages=3 category=spices\n{BASE}pantry/herbs pages=2 category=herbs\n{BASE}milk category=milk\n"
    )
    snapshot, rows = page_limits_check.check_foodstuffs("paknsave", str(cats))
    flagged = {r["label"] for r in rows if r["full_at_limit"]}
    assert flagged == {"spices", "milk"}
    assert {r["label"]: r["last_page_items"] for r in rows} == {"spices": 50, "herbs": 0, "milk": 50}
    before = sorted(p.name for p in tmp_path.rglob("*"))
    page_limits_check.report("paknsave", str(cats))
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
