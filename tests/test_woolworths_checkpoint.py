"""A Woolworths run cut off part way (power cut) carries on from the last finished shelf or
search-term batch; a different day, a changed list, a damaged file or a dry run starts fresh."""

from __future__ import annotations

import sys
from datetime import date

import pytest

from scraper import category_tree, checkpoint, woolworths
from scraper.ingredients import StoreLists
from scraper.price_runs import RunResult
from tests.test_woolworths_flow import _p

PAGES = [woolworths.BrowsePage(f"pantry/shelf-{n}", f"shelf-{n}", (f"c{n}",), 2) for n in (1, 2, 3, 4)]


class PowerCut(BaseException):
    """Stands in for the Pi losing power: nothing after it runs, nothing is cleaned up."""


@pytest.fixture(autouse=True)
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(category_tree, "STATE_DIR", str(tmp_path / "data"))


def _browse_mock(monkeypatch, cut_at=None):
    loaded = []

    def fake(path, max_pages, label, stat=None):
        if path == cut_at:
            raise PowerCut
        loaded.append(path)
        n = path.rsplit("-", 1)[1]
        return [_p(f"b{n}", f"Item {n}")], [], 1

    monkeypatch.setattr(woolworths, "browse_products", fake)
    return loaded


def _search_mock(monkeypatch, cut_at=None):
    searched = []

    def fake(term, size):
        if term == cut_at:
            raise PowerCut
        searched.append(term)
        return [_p(f"s-{term}", term)], []

    monkeypatch.setattr(woolworths, "search_products", fake)
    return searched


# --- browse -----------------------------------------------------------------------------------


def test_browse_saves_after_every_shelf(monkeypatch):
    _browse_mock(monkeypatch, cut_at="pantry/shelf-3")
    with pytest.raises(PowerCut):
        woolworths.browse_all(PAGES, progress=True, scraped_at="T0")
    saved = checkpoint.read_today("woolworths")["sections"]["browse"]["state"]
    assert saved["done"] == 2 and sorted(p["product_id"] for p in saved["products"]) == ["b1", "b2"]


def test_browse_carries_on_after_the_last_finished_shelf(monkeypatch):
    _browse_mock(monkeypatch, cut_at="pantry/shelf-3")
    with pytest.raises(PowerCut):
        woolworths.browse_all(PAGES, progress=True, scraped_at="T0")

    loaded = _browse_mock(monkeypatch)
    result = woolworths.browse_all(PAGES, progress=True, scraped_at="T1")

    assert loaded == ["pantry/shelf-3", "pantry/shelf-4"]
    assert sorted(p["product_id"] for p in result["products"]) == ["b1", "b2", "b3", "b4"]
    assert result["scraped_at"] == "T0"  # the run's first start time is kept
    assert result["stats"]["categories_scraped"] == 4
    assert [s.label for s in result["page_stats"]] == [f"shelf-{n}" for n in (1, 2, 3, 4)]
    assert set(result["scraped_categories"]) == {f"shelf-{n}" for n in (1, 2, 3, 4)}
    assert result["scraped_categories"]["shelf-1"] == ("c1",)


def test_browse_starts_fresh_when_the_shelf_list_changed(monkeypatch):
    _browse_mock(monkeypatch, cut_at="pantry/shelf-3")
    with pytest.raises(PowerCut):
        woolworths.browse_all(PAGES, progress=True, scraped_at="T0")
    loaded = _browse_mock(monkeypatch)
    woolworths.browse_all(PAGES[:3] + [woolworths.BrowsePage("pantry/shelf-9", "shelf-9")], progress=True, scraped_at="T1")
    assert loaded[0] == "pantry/shelf-1" and len(loaded) == 4


def test_browse_starts_fresh_when_a_page_limit_changed(monkeypatch):
    _browse_mock(monkeypatch, cut_at="pantry/shelf-3")
    with pytest.raises(PowerCut):
        woolworths.browse_all(PAGES, progress=True, scraped_at="T0")
    loaded = _browse_mock(monkeypatch)
    longer = [woolworths.BrowsePage(p.path, p.label, p.covers, 9) for p in PAGES]
    woolworths.browse_all(longer, progress=True, scraped_at="T1")
    assert len(loaded) == 4


def test_browse_starts_fresh_on_another_day(monkeypatch):
    _browse_mock(monkeypatch, cut_at="pantry/shelf-3")
    with pytest.raises(PowerCut):
        woolworths.browse_all(PAGES, progress=True, scraped_at="T0")

    class Tomorrow(date):
        @classmethod
        def today(cls):
            return date.fromordinal(date.today().toordinal() + 1)

    monkeypatch.setattr(checkpoint, "date", Tomorrow)
    loaded = _browse_mock(monkeypatch)
    woolworths.browse_all(PAGES, progress=True, scraped_at="T1")
    assert len(loaded) == 4


@pytest.mark.parametrize("damage", ["not json at all {", '{"store": "woolworths"}', "[1, 2]", ""])
def test_browse_starts_fresh_when_the_file_is_damaged(monkeypatch, damage):
    path = checkpoint.checkpoint_path("woolworths")
    path.parent.mkdir(parents=True)
    path.write_text(damage)
    loaded = _browse_mock(monkeypatch)
    result = woolworths.browse_all(PAGES, progress=True, scraped_at="T1")
    assert len(loaded) == 4 and len(result["products"]) == 4


def test_browse_starts_fresh_when_the_saved_state_is_missing_parts(monkeypatch):
    sig = woolworths.browse_signature(PAGES)
    checkpoint.save_section("woolworths", "browse", sig, {"done": 2, "products": []})
    loaded = _browse_mock(monkeypatch)
    woolworths.browse_all(PAGES, progress=True, scraped_at="T1")
    assert len(loaded) == 4


def test_a_dry_run_neither_saves_nor_resumes(monkeypatch):
    _browse_mock(monkeypatch, cut_at="pantry/shelf-3")
    with pytest.raises(PowerCut):
        woolworths.browse_all(PAGES, progress=False, scraped_at="T0")
    assert not checkpoint.exists_today("woolworths")

    _browse_mock(monkeypatch, cut_at="pantry/shelf-3")
    with pytest.raises(PowerCut):
        woolworths.browse_all(PAGES, progress=True, scraped_at="T0")
    loaded = _browse_mock(monkeypatch)
    woolworths.browse_all(PAGES, progress=False, scraped_at="T1")
    assert len(loaded) == 4  # a dry run ignores the file...
    assert checkpoint.exists_today("woolworths")  # ...and leaves it alone


def test_a_failed_shelf_still_counts_as_done_so_the_run_moves_on(monkeypatch):
    def fake(path, max_pages, label, stat=None):
        if path == "pantry/shelf-2":
            raise RuntimeError("boom")
        return [_p(path[-1], path)], [], 1

    monkeypatch.setattr(woolworths, "browse_products", fake)
    woolworths.browse_all(PAGES[:2], progress=True, scraped_at="T0")
    saved = checkpoint.read_today("woolworths")["sections"]["browse"]["state"]
    assert saved["done"] == 2 and saved["failed"] == 1


# --- search -----------------------------------------------------------------------------------

TERMS = ["rice", "sugar", "flour", "salt", "oil", "tea"]


def test_search_saves_every_batch_and_carries_on(monkeypatch):
    monkeypatch.setattr(woolworths, "SEARCH_SAVE_EVERY", 2)
    _search_mock(monkeypatch, cut_at="salt")
    collected: list[dict] = []
    with pytest.raises(PowerCut):
        woolworths.search_all(TERMS, 30, None, collected, progress=True)
    saved = checkpoint.read_today("woolworths")["sections"]["search"]["state"]
    assert saved["done"] == 2 and saved["searched"] == ["rice", "sugar"]  # last save was before "flour"

    searched = _search_mock(monkeypatch)
    collected = []
    outcome, blocked = woolworths.search_all(TERMS, 30, None, collected, progress=True)

    assert searched == ["flour", "salt", "oil", "tea"]
    assert outcome.searched == TERMS and sorted(outcome.found) == sorted(TERMS)
    assert sorted(p["product_id"] for p in collected) == sorted(f"s-{t}" for t in TERMS)
    assert blocked == []
    final = checkpoint.read_today("woolworths")["sections"]["search"]["state"]
    assert final["done"] == len(TERMS)


def test_search_takes_the_time_already_used_off_the_budget(monkeypatch):
    monkeypatch.setattr(woolworths, "SEARCH_SAVE_EVERY", 2)
    checkpoint.save_section("woolworths", "search", checkpoint.signature(TERMS), {
        "done": 2, "searched": ["rice", "sugar"], "found": {}, "not_found": ["rice", "sugar"],
        "unreported": [], "blocked": [], "collected": [], "spent": 1000.0,
    })
    searched = _search_mock(monkeypatch)
    now = woolworths.time.monotonic()
    outcome, _ = woolworths.search_all(TERMS, 30, now + 900, [], progress=True)  # 900s left, 1000s already spent
    assert searched == [] and outcome.unsearched == TERMS[2:]


def test_search_starts_fresh_when_the_term_list_changed(monkeypatch):
    monkeypatch.setattr(woolworths, "SEARCH_SAVE_EVERY", 2)
    _search_mock(monkeypatch, cut_at="salt")
    with pytest.raises(PowerCut):
        woolworths.search_all(TERMS, 30, None, [], progress=True)
    searched = _search_mock(monkeypatch)
    woolworths.search_all(TERMS + ["honey"], 30, None, [], progress=True)
    assert searched == TERMS + ["honey"]


def test_search_starts_fresh_for_a_dry_run_and_a_damaged_file(monkeypatch):
    monkeypatch.setattr(woolworths, "SEARCH_SAVE_EVERY", 2)
    _search_mock(monkeypatch, cut_at="salt")
    with pytest.raises(PowerCut):
        woolworths.search_all(TERMS, 30, None, [], progress=True)
    searched = _search_mock(monkeypatch)
    woolworths.search_all(TERMS, 30, None, [], progress=False)
    assert searched == TERMS

    checkpoint.checkpoint_path("woolworths").write_text("{broken")
    searched = _search_mock(monkeypatch)
    woolworths.search_all(TERMS, 30, None, [], progress=True)
    assert searched == TERMS


# --- the whole run ----------------------------------------------------------------------------


def _main(monkeypatch, tmp_path, argv_extra=("--save",)):
    published, reported = [], []
    lists = StoreLists(store="woolworths", search_terms=["rice", "sugar", "flour"], watch_terms=[], skip=frozenset())
    monkeypatch.setattr(woolworths, "fetch_store_lists", lambda store, url: lists)
    monkeypatch.setattr(woolworths, "publish_run", lambda products, *a, **kw: published.append(products) or RunResult(items=len(products)))
    monkeypatch.setattr(woolworths, "report_outcome", lambda store, o: reported.append(list(o.found)))
    cats = tmp_path / "c.txt"
    cats.write_text("pantry/shelf-1\npantry/shelf-2\n")
    monkeypatch.setattr(sys, "argv", ["x", "--categories", str(cats), "--snapshots-dir", str(tmp_path / "snap"),
                                      "--history-path", str(tmp_path / "h.json"), *argv_extra])
    return woolworths.main(), published, reported


def test_a_resumed_run_posts_everything_and_removes_the_progress_file(monkeypatch, tmp_path, capsys):
    pages = [woolworths.BrowsePage("pantry/shelf-1", "shelf-1"), woolworths.BrowsePage("pantry/shelf-2", "shelf-2")]
    checkpoint.save_section("woolworths", "browse", woolworths.browse_signature(pages), {
        "done": 2, "products": [_p("b1", "Item 1"), _p("b2", "Item 2")], "dropped": [], "blocked": [], "failed": 0,
        "scraped": {"shelf-1": [], "shelf-2": []}, "page_stats": [], "scraped_at": "2026-11-25T08:00:00+13:00",
    })
    checkpoint.save_section("woolworths", "search", checkpoint.signature(["rice", "sugar", "flour"]), {
        "done": 2, "searched": ["rice", "sugar"], "found": {"rice": [_p("s-rice", "rice")], "sugar": [_p("s-sugar", "sugar")]},
        "not_found": [], "unreported": [], "blocked": [], "collected": [_p("s-rice", "rice"), _p("s-sugar", "sugar")], "spent": 60.0,
    })
    browsed = _browse_mock(monkeypatch)
    searched = _search_mock(monkeypatch)
    scraped_at = []
    monkeypatch.setattr(woolworths, "write_scrape_meta", lambda path, at: scraped_at.append(at))

    code, published, reported = _main(monkeypatch, tmp_path)
    out = capsys.readouterr().out

    assert code == 0
    assert browsed == [] and searched == ["flour"]
    assert sorted(p["product_id"] for p in published[0]) == ["b1", "b2", "s-flour", "s-rice", "s-sugar"]
    assert reported == [["rice", "sugar", "flour"]]
    assert set(scraped_at) == {"2026-11-25T08:00:00+13:00"}
    assert "Found progress saved earlier today" in out
    assert "Resuming the browse step after shelf 2 of 2" in out
    assert "Resuming the search step after term 2 of 3" in out
    assert not checkpoint.exists_today("woolworths")
    assert list((tmp_path / "snap").glob("*.json"))


def test_a_run_that_is_cut_off_mid_search_leaves_its_progress_for_the_next_one(monkeypatch, tmp_path):
    monkeypatch.setattr(woolworths, "SEARCH_SAVE_EVERY", 1)
    _browse_mock(monkeypatch)
    _search_mock(monkeypatch, cut_at="flour")
    with pytest.raises(PowerCut):
        _main(monkeypatch, tmp_path)
    assert checkpoint.exists_today("woolworths")

    browsed = _browse_mock(monkeypatch)
    searched = _search_mock(monkeypatch)
    code, published, _ = _main(monkeypatch, tmp_path)
    assert code == 0 and browsed == [] and searched == ["flour"]
    assert len(published[0]) == 5
    assert not checkpoint.exists_today("woolworths")


def test_a_dry_run_writes_no_progress_file(monkeypatch, tmp_path):
    _browse_mock(monkeypatch)
    _search_mock(monkeypatch)
    code, published, _ = _main(monkeypatch, tmp_path, argv_extra=())
    assert code == 0 and published == []
    assert not checkpoint.exists_today("woolworths")
