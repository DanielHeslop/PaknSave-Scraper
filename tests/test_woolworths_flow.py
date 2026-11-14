import time

import pytest

from scraper import woolworths
from scraper.ingredients import StoreLists
from scraper.price_runs import RunResult
from scraper.woolworths_transport import BotDetected, browse_url_filters


def _p(pid, name, price=2.0):
    return {
        "product_id": pid, "name": name, "category": "x", "size": "1kg", "price": price,
        "unit_price": 2.0, "unit": "kg", "scraped_at": "2026-01-01", "supermarket": "Woolworths",
    }


def test_browse_filters_for_aisle_and_shelf():
    assert browse_url_filters("pantry/baking") == [
        ("dasFilter", "Department;;pantry;false"), ("dasFilter", "Aisle;;baking;false")]
    assert browse_url_filters("/household/kitchen/kitchen-cleaners/")[2] == ("dasFilter", "Shelf;;kitchen-cleaners;false")
    with pytest.raises(ValueError):
        browse_url_filters("a/b/c/d")


def test_categories_file_parses_options(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("# hi\npantry/baking category=baking covers=Sugar,flour pages=2\nhousehold/kitchen/kitchen-cleaners\n")
    pages = woolworths.parse_categories_file(f)
    assert pages[0] == woolworths.BrowsePage("pantry/baking", "baking", ("sugar", "flour"), 2)
    assert pages[1].label == "kitchen-cleaners" and pages[1].max_pages == 5


def test_shipped_categories_file_has_the_six_watch_paths():
    paths = {p.path for p in woolworths.parse_categories_file(woolworths.DEFAULT_CATEGORIES_PATH)}
    assert paths == {
        "pantry/baking", "pantry/oil-vinegar-condiments", "pantry/tinned-foods-packets", "pantry/bulk-foods",
        "household/kitchen/kitchen-cleaners", "household/bathroom/toilet-bathroom-cleaners"}


def test_browse_all_dedupes_and_survives_a_bad_path(monkeypatch):
    def fake(path, max_pages, label):
        if path == "bad":
            raise RuntimeError("boom")
        return [_p("1", "Sugar"), _p("2", "Flour")], [], 2
    monkeypatch.setattr(woolworths, "browse_products", fake)
    result = woolworths.browse_all([
        woolworths.BrowsePage("a", "a"), woolworths.BrowsePage("bad", "bad"), woolworths.BrowsePage("b", "b")])
    assert [p["product_id"] for p in result["products"]] == ["1", "2"]
    assert result["stats"]["categories_scraped"] == 2 and result["stats"]["categories_failed"] == 1


def test_search_all_found_not_found_and_failures(monkeypatch):
    def fake(term, size):
        if term == "rice":
            return [_p("1", "Basmati Rice"), _p("2", "Unrelated Thing")], []
        if term == "saffron":
            return [_p("3", "Unrelated Thing")], []
        if term == "oops":
            raise RuntimeError("500")
        return [], []
    monkeypatch.setattr(woolworths, "search_products", fake)
    outcome, blocked = woolworths.search_all(["rice", "saffron", "wine", "oops"], 30)
    assert list(outcome.found) == ["rice"]
    assert [p["product_id"] for p in outcome.found["rice"]] == ["1"]
    assert outcome.not_found == ["saffron", "wine"]
    assert outcome.unreported == ["oops"] and blocked == []


def test_search_all_stops_on_bot_detection(monkeypatch):
    def fake(term, size):
        if term == "beans":
            raise BotDetected("429")
        return [_p("1", term)], []
    monkeypatch.setattr(woolworths, "search_products", fake)
    outcome, blocked = woolworths.search_all(["apples", "beans", "carrots"], 30)
    assert list(outcome.found) == ["apples"] and outcome.unsearched == ["beans", "carrots"] and len(blocked) == 1


def test_search_all_respects_deadline(monkeypatch):
    monkeypatch.setattr(woolworths, "search_products", lambda t, size: pytest.fail("should not search"))
    outcome, _ = woolworths.search_all(["apples"], 30, deadline=time.monotonic() - 1)
    assert outcome.unsearched == ["apples"]


def _run_main(monkeypatch, tmp_path, *, search_raises=False, save=True):
    published, reported = [], []
    monkeypatch.setattr(woolworths, "browse_products", lambda path, mp, label: ([_p("1", "Sugar")], [], 1))
    monkeypatch.setattr(woolworths, "search_products", lambda t, size: ([_p("1", "Sugar"), _p("9", "Rice")], []))
    lists = StoreLists(store="woolworths", search_terms=["rice", "sugar", "skipme"], watch_terms=["soap"],
                       skip=frozenset({"skipme"}))
    def getter(store, url):
        if search_raises:
            raise RuntimeError("list exploded")
        return lists
    monkeypatch.setattr(woolworths, "fetch_store_lists", getter)
    monkeypatch.setattr(woolworths, "publish_run", lambda products, *a, **kw: published.append(products) or RunResult(items=len(products)))
    monkeypatch.setattr(woolworths, "report_outcome", lambda store, o: reported.append((store, list(o.found), o.not_found)))
    cats = tmp_path / "c.txt"; cats.write_text("pantry/baking\n")
    argv = ["--categories", str(cats), "--snapshots-dir", str(tmp_path / "snap"), "--history-path", str(tmp_path / "h.json")]
    monkeypatch.setattr(sys_argv(), "argv", ["x", *argv, *(["--save"] if save else [])])
    return woolworths.main(), published, reported


def sys_argv():
    import sys
    return sys


def test_main_publishes_deduped_and_reports(monkeypatch, tmp_path, capsys):
    code, published, reported = _run_main(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert code == 0
    assert sorted(p["product_id"] for p in published[0]) == ["1", "9"]
    assert reported == [("woolworths", ["rice", "sugar"], ["soap"])]
    assert out.strip().splitlines()[-1].startswith("SUMMARY: products published 2, terms searched 3, found 2, not found 1, skipped 1")


def test_main_publishes_even_if_search_step_fails(monkeypatch, tmp_path, capsys):
    code, published, _ = _run_main(monkeypatch, tmp_path, search_raises=True)
    assert code == 0 and [p["product_id"] for p in published[0]] == ["1"]
    assert "carrying on to publish" in capsys.readouterr().out


def test_dry_run_publishes_and_reports_nothing(monkeypatch, tmp_path):
    code, published, reported = _run_main(monkeypatch, tmp_path, save=False)
    assert code == 0 and published == [] and reported == []
