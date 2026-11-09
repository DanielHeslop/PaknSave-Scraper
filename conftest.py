import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


import pytest


@pytest.fixture(autouse=True)
def _no_real_robots_fetch(monkeypatch):
    """Tests must never fetch a real robots.txt; default to allow-all."""
    import scraper.robots as robots

    monkeypatch.setattr(robots, "fetch_rules", lambda site_root, label, session=None: robots.RobotsRules())
