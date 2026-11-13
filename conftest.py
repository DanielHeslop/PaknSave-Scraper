import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


import pytest


@pytest.fixture(autouse=True)
def _no_real_robots_fetch(monkeypatch):
    """Tests must never fetch a real robots.txt; default to allow-all."""
    import scraper.robots as robots

    monkeypatch.setattr(robots, "fetch_rules", lambda site_root, label, session=None: robots.RobotsRules())


@pytest.fixture(autouse=True)
def _no_real_app_matcher(monkeypatch):
    """Tests must never call the real app; the matcher env is unset unless a test sets it."""
    monkeypatch.delenv("MAILBOX_URL", raising=False)
    monkeypatch.delenv("MAILBOX_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _isolated_category_state(monkeypatch, tmp_path):
    """Category trees and mappings go to a temp dir, and no test reads a real site's tree."""
    import scraper.category_tree as category_tree

    monkeypatch.setattr(category_tree, "STATE_DIR", str(tmp_path / "state"))

    def offline(store, guard=None):
        raise RuntimeError("offline in tests")

    monkeypatch.setattr(category_tree, "fetch_tree", offline)
