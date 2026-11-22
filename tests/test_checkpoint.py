"""A scrape cut off part way (the Pi lost power during the 22 Nov 2026 Pak'nSave run) carries on
from the last finished category instead of starting again."""

from __future__ import annotations

import pytest

from scraper import category_tree, checkpoint
from tests.test_watch_pagination import BASE, FakeSite, _ids, _scrape

CATEGORIES = "\n".join(f"{BASE}pantry/shelf-{n} category=shelf-{n}" for n in (1, 2, 3, 4)) + "\n"


def _url(n: int) -> str:
    return BASE + f"pantry/shelf-{n}"


@pytest.fixture(autouse=True)
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(category_tree, "STATE_DIR", str(tmp_path / "data"))


def _site(**kw) -> FakeSite:
    return FakeSite({_url(n): _ids(f"s{n}x", 5) for n in (1, 2, 3, 4)}, **kw)


def _cut_off_run(monkeypatch, tmp_path):
    site = _site()
    site.crash = {_url(3)}
    with pytest.raises(RuntimeError):
        _scrape(monkeypatch, tmp_path, site, categories=CATEGORIES, checkpoint=True)
    return site


def test_a_cut_off_scrape_carries_on_after_the_last_finished_category(monkeypatch, tmp_path):
    first = _cut_off_run(monkeypatch, tmp_path)
    assert first.loaded == [_url(1), _url(2)]

    second = _site()
    result = _scrape(monkeypatch, tmp_path, second, categories=CATEGORIES, checkpoint=True)

    assert second.loaded == [_url(3), _url(4)]
    assert second.pauses == 1  # no pause before the first page of the new browser, as at the start of any run
    assert sorted(p["product_id"] for p in result["products"]) == sorted(
        i for n in (1, 2, 3, 4) for i in _ids(f"s{n}x", 5)
    )
    assert result["stats"]["pages_scraped"] == 4
    assert result["stats"]["categories_scraped"] == 4
    assert [s.label for s in result["page_stats"]] == ["shelf-1", "shelf-2", "shelf-3", "shelf-4"]


def test_the_carried_on_run_keeps_the_time_the_scrape_began(monkeypatch, tmp_path):
    _cut_off_run(monkeypatch, tmp_path)
    saved = checkpoint.load("paknsave", [_url(n) for n in (1, 2, 3, 4)])
    result = _scrape(monkeypatch, tmp_path, _site(), categories=CATEGORIES, checkpoint=True)
    assert result["scraped_at"] == saved["scraped_at_nz"]


def test_clearing_the_checkpoint_makes_the_next_run_start_afresh(monkeypatch, tmp_path):
    _cut_off_run(monkeypatch, tmp_path)
    checkpoint.clear("paknsave")
    fresh = _site()
    _scrape(monkeypatch, tmp_path, fresh, categories=CATEGORIES, checkpoint=True)
    assert fresh.loaded == [_url(n) for n in (1, 2, 3, 4)]


def test_a_changed_category_list_starts_afresh(monkeypatch, tmp_path):
    _cut_off_run(monkeypatch, tmp_path)
    fresh = _site()
    _scrape(monkeypatch, tmp_path, fresh, categories=CATEGORIES + f"{_url(5)}\n", checkpoint=True)
    assert fresh.loaded[0] == _url(1)


def test_yesterdays_checkpoint_is_ignored(monkeypatch, tmp_path):
    _cut_off_run(monkeypatch, tmp_path)
    path = checkpoint.checkpoint_path("paknsave")
    path.write_text(path.read_text().replace('"day": "', '"day": "1999-'))
    fresh = _site()
    _scrape(monkeypatch, tmp_path, fresh, categories=CATEGORIES, checkpoint=True)
    assert fresh.loaded[0] == _url(1)


def test_a_damaged_checkpoint_is_ignored(monkeypatch, tmp_path):
    _cut_off_run(monkeypatch, tmp_path)
    checkpoint.checkpoint_path("paknsave").write_text("{not json")
    fresh = _site()
    _scrape(monkeypatch, tmp_path, fresh, categories=CATEGORIES, checkpoint=True)
    assert fresh.loaded[0] == _url(1)


def test_a_dry_run_neither_writes_nor_reads_a_checkpoint(monkeypatch, tmp_path):
    _scrape(monkeypatch, tmp_path, _site(), categories=CATEGORIES)
    assert not checkpoint.checkpoint_path("paknsave").exists()
    _cut_off_run(monkeypatch, tmp_path)
    dry = _site()
    _scrape(monkeypatch, tmp_path, dry, categories=CATEGORIES)
    assert dry.loaded[0] == _url(1)


def test_a_checkpoint_after_the_last_category_leaves_nothing_to_read(monkeypatch, tmp_path):
    _scrape(monkeypatch, tmp_path, _site(), categories=CATEGORIES, checkpoint=True)
    again = _site()
    result = _scrape(monkeypatch, tmp_path, again, categories=CATEGORIES, checkpoint=True)
    assert again.loaded == []
    assert len(result["products"]) == 20
