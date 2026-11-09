"""Tests for the per-store search-list client in scraper/ingredients.py."""

from __future__ import annotations

import json
import urllib.error

import pytest

import scraper.ingredients as ingredients
from scraper.ingredients import (
    StoreLists,
    fetch_ingredient_list,
    fetch_store_lists,
    report_found,
    report_not_found,
)

URL = "https://example.com/api/ingredient-list"
TOKEN = "super-secret-test-token"

_PAYLOAD = {
    "store": "paknsave",
    "generatedAt": "2026-11-10T00:00:00Z",
    "search": [{"term": "garlic", "lines": 19}, {"term": "Olive  Oil", "lines": 7}, {"term": "garlic", "lines": 1}],
    "watch": {"terms": ["toilet paper"], "categories": ["baking", "bulk foods"]},
    "skip": ["Saffron", "  star   anise "],
}


class _Resp:
    def __init__(self, body, status=200):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("MAILBOX_TOKEN", TOKEN)


def _install(monkeypatch, responder):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return responder(request)

    monkeypatch.setattr(ingredients.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_fetch_store_lists_parses_order_watch_and_skip(monkeypatch):
    calls = _install(monkeypatch, lambda r: _Resp(_PAYLOAD))
    lists = fetch_store_lists("paknsave", URL)

    assert isinstance(lists, StoreLists)
    assert lists.search_terms == ["garlic", "Olive  Oil"]
    assert lists.watch_terms == ["toilet paper"]
    assert lists.watch_categories == ["baking", "bulk foods"]
    assert lists.is_skipped("saffron")
    assert lists.is_skipped("STAR anise")
    assert not lists.is_skipped("garlic")
    assert calls[0].full_url == URL + "?store=paknsave"
    assert calls[0].get_header("Authorization") == f"Bearer {TOKEN}"


def test_fetch_store_lists_keeps_existing_query_and_replaces_store(monkeypatch):
    calls = _install(monkeypatch, lambda r: _Resp(_PAYLOAD))
    fetch_store_lists("newworld", URL + "?a=1&store=old")
    assert calls[0].full_url == URL + "?a=1&store=newworld"


def test_fetch_store_lists_accepts_plain_string_terms_and_missing_sections(monkeypatch):
    _install(monkeypatch, lambda r: _Resp({"search": ["milk", "eggs"]}))
    lists = fetch_store_lists("woolworths", URL)
    assert lists.search_terms == ["milk", "eggs"]
    assert lists.watch_terms == [] and lists.watch_categories == [] and lists.skip == frozenset()


def test_fetch_store_lists_http_error_fails_open(monkeypatch, capsys):
    def boom(request):
        raise urllib.error.HTTPError(URL, 401, "Unauthorized", {}, None)

    _install(monkeypatch, boom)
    assert fetch_store_lists("paknsave", URL) is None
    out = capsys.readouterr().out
    assert "continuing without the search step" in out
    assert "401" in out
    assert TOKEN not in out


@pytest.mark.parametrize(
    "responder",
    [
        lambda r: (_ for _ in ()).throw(urllib.error.URLError("no route")),
        lambda r: (_ for _ in ()).throw(TimeoutError()),
        lambda r: _Resp(b"not json"),
        lambda r: _Resp(["old", "style", "list"]),
        lambda r: _Resp({"search": "nope"}),
        lambda r: _Resp({"search": [{"lines": 3}]}),
        lambda r: _Resp({"search": [], "watch": []}),
        lambda r: _Resp({}, status=500),
    ],
)
def test_fetch_store_lists_never_raises(monkeypatch, responder):
    _install(monkeypatch, responder)
    assert fetch_store_lists("paknsave", URL) is None


def test_fetch_store_lists_without_token_sends_no_auth_header(monkeypatch):
    monkeypatch.delenv("MAILBOX_TOKEN", raising=False)
    calls = _install(monkeypatch, lambda r: _Resp(_PAYLOAD))
    fetch_store_lists("paknsave", URL)
    assert calls[0].get_header("Authorization") is None


def test_report_not_found_posts_terms_and_timestamp(monkeypatch):
    calls = _install(monkeypatch, lambda r: _Resp({"ok": True}))
    assert report_not_found("newworld", ["saffron", "yuzu"], URL, searched_at="2026-11-10T01:02:03+00:00") is True
    req = calls[0]
    assert req.get_method() == "POST"
    assert req.full_url == URL + "?store=newworld&action=not-found"
    assert json.loads(req.data) == {"terms": ["saffron", "yuzu"], "searchedAt": "2026-11-10T01:02:03+00:00"}
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert req.get_header("Content-type") == "application/json"


def test_report_not_found_defaults_searched_at_to_iso_now(monkeypatch):
    calls = _install(monkeypatch, lambda r: _Resp({}))
    report_not_found("paknsave", ["saffron"], URL)
    searched_at = json.loads(calls[0].data)["searchedAt"]
    assert "T" in searched_at and searched_at.endswith("+00:00")


def test_report_found_posts_terms_only(monkeypatch):
    calls = _install(monkeypatch, lambda r: _Resp({}))
    assert report_found("woolworths", ["garlic"], URL) is True
    assert calls[0].full_url == URL + "?store=woolworths&action=found"
    assert json.loads(calls[0].data) == {"terms": ["garlic"]}


def test_reports_with_no_terms_make_no_request(monkeypatch):
    calls = _install(monkeypatch, lambda r: _Resp({}))
    assert report_not_found("paknsave", [], URL) is True
    assert report_found("paknsave", [], URL) is True
    assert calls == []


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.HTTPError(URL, 500, "Server Error", {}, None),
        urllib.error.URLError("down"),
        TimeoutError(),
        RuntimeError("weird"),
    ],
)
def test_report_failures_never_raise_and_never_leak_token(monkeypatch, capsys, exc):
    def boom(request):
        raise exc

    _install(monkeypatch, boom)
    assert report_not_found("paknsave", ["x"], URL) is False
    assert report_found("paknsave", ["x"], URL) is False
    out = capsys.readouterr().out
    assert "failed" in out
    assert TOKEN not in out


def test_report_non_2xx_status_is_a_failure(monkeypatch):
    _install(monkeypatch, lambda r: _Resp({}, status=302))
    assert report_found("paknsave", ["x"], URL) is False


def test_old_fetch_ingredient_list_still_works(monkeypatch):
    _install(monkeypatch, lambda r: _Resp(["milk", "eggs"]))
    assert fetch_ingredient_list(URL) == ["milk", "eggs"]
