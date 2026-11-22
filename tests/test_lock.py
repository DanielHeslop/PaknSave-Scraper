"""Only one scrape runs at a time."""

from __future__ import annotations

import subprocess
import sys

import pytest

from scraper import category_tree, lock, woolworths


@pytest.fixture(autouse=True)
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(category_tree, "STATE_DIR", str(tmp_path / "data"))


def test_a_second_scrape_is_refused_while_the_first_holds_the_lock():
    with lock.scrape_lock("woolworths scrape"):
        assert lock.is_held()
        with pytest.raises(lock.ScrapeLocked) as refused:
            with lock.scrape_lock("newworld scrape"):
                pytest.fail("two scrapes at once")
        assert "woolworths scrape" in str(refused.value)


def test_the_lock_is_released_afterwards_and_after_an_error():
    with lock.scrape_lock("one"):
        pass
    assert not lock.is_held()
    with pytest.raises(ValueError):
        with lock.scrape_lock("two"):
            raise ValueError
    assert not lock.is_held()
    with lock.scrape_lock("three"):
        pass


def test_run_locked_returns_the_exit_code_or_75_with_a_plain_message():
    lines = []
    assert lock.run_locked("a", lambda: 0, lines.append) == 0 and lines == []
    with lock.scrape_lock("holder scrape"):
        assert lock.run_locked("b", lambda: pytest.fail("ran"), lines.append) == lock.EXIT_LOCKED == 75
    assert "Not started: holder scrape" in lines[0] and "Only one scrape runs at a time" in lines[0]


def test_a_lock_left_by_a_killed_process_does_not_block(tmp_path):
    code = (
        "import sys; from scraper import category_tree, lock; "
        f"category_tree.STATE_DIR = {str(tmp_path / 'data')!r}; "
        "cm = lock.scrape_lock('doomed'); cm.__enter__(); import os; os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    assert lock.lock_path().read_text().startswith("doomed")  # the file is left behind, as after a power cut
    assert not lock.is_held()
    with lock.scrape_lock("next"):
        pass


def test_a_lock_held_by_another_process_blocks_this_one(tmp_path):
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; from scraper import category_tree, lock; "
         f"category_tree.STATE_DIR = {str(tmp_path / 'data')!r}; "
         "cm = lock.scrape_lock('other process'); cm.__enter__(); print('ready', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        assert lock.is_held()
        with pytest.raises(lock.ScrapeLocked):
            with lock.scrape_lock("me"):
                pass
    finally:
        holder.kill()
        holder.wait()


def test_the_woolworths_entry_point_does_not_start_while_another_scrape_runs(monkeypatch, capsys):
    monkeypatch.setattr(woolworths, "browse_products", lambda *a, **k: pytest.fail("scraped"))
    monkeypatch.setattr(sys, "argv", ["x", "--save"])
    with lock.scrape_lock("newworld scrape"):
        assert woolworths.main() == 75
    assert "Not started: newworld scrape" in capsys.readouterr().out


def test_the_category_scrapers_do_not_start_while_another_scrape_runs(monkeypatch, capsys):
    from scraper import run, weekly_combined

    for module in (weekly_combined, run):
        monkeypatch.setattr(module, "main_async", lambda args: pytest.fail("scraped"))
        monkeypatch.setattr(sys, "argv", ["x", "--save"])
        with lock.scrape_lock("woolworths scrape"):
            with pytest.raises(SystemExit) as exit_:
                module.main()
        assert exit_.value.code == 75
    assert capsys.readouterr().out.count("Not started: woolworths scrape") == 2
