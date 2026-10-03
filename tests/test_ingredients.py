"""Tests for scraper/ingredients.py's fetch_ingredient_list, in particular
the Bearer auth added after the 2026-09-27 / 2026-10-03 weekly-combined runs
started getting 401s from the app's ingredient-list endpoint - every other
call this scraper makes to the app (price-mailbox, scrape-exclusions)
already sent this same token; ingredients.py was the one place that didn't.
"""

from __future__ import annotations

from scraper.ingredients import fetch_ingredient_list


class _FakeResponse:
    def __init__(self, status=200, body=b"[]"):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _run_fetch(monkeypatch, token=None):
    """Run fetch_ingredient_list with a fake transport, returning the
    Authorization header the request actually carried (or None if absent).
    """
    if token is not None:
        monkeypatch.setenv("MAILBOX_TOKEN", token)
    else:
        monkeypatch.delenv("MAILBOX_TOKEN", raising=False)

    captured = {}

    def _fake_urlopen(request, timeout=None):
        captured["auth_header"] = request.get_header("Authorization")
        return _FakeResponse(body=b'["Milk", "Eggs"]')

    monkeypatch.setattr("scraper.ingredients.urllib.request.urlopen", _fake_urlopen)
    names = fetch_ingredient_list("https://example.com/api/ingredient-list")
    return captured["auth_header"], names


def test_sends_bearer_header_when_mailbox_token_set(monkeypatch):
    auth_header, names = _run_fetch(monkeypatch, token="test-token")
    assert auth_header == "Bearer test-token"
    assert names == ["Milk", "Eggs"]


def test_no_authorization_header_when_mailbox_token_unset(monkeypatch):
    auth_header, _ = _run_fetch(monkeypatch, token=None)
    assert auth_header is None


def test_no_token_in_log_output(monkeypatch, capsys):
    token = "test-token"
    _run_fetch(monkeypatch, token=token)
    out = capsys.readouterr().out
    assert token not in out
