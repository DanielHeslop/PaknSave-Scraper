"""Authorization header, the run-list (index) parser, Retry-After on 503, and the 401 log.

Nothing here touches the network: urlopen is replaced by a small fake.
"""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

import pytest

import scraper.ingredients as ingredients
import scraper.price_runs as price_runs
from scraper.auth import bearer, clean_token
from scraper.price_runs import (
    parse_index,
    parse_retry_after,
    post_part,
    verify_in_index,
)

TOKEN = "secret-token-xyz"
RUNS = "https://app.example/api/price-runs"
LIST = "https://app.example/api/ingredient-list"
RUN_ID = "newworld-2026-11-13-32f6936f5e"


class Reply:
    def __init__(self, body: dict | list | None = None, status: int = 200):
        self.status = status
        self._body = json.dumps({} if body is None else body).encode()

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code: int, headers: dict | None = None, body: bytes = b"{}"):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(RUNS, code, "Reply", message, io.BytesIO(body))


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(price_runs, "_sleep", recorded.append)
    monkeypatch.setattr(price_runs, "log", lambda message: logs.append(message))
    logs.clear()
    return recorded


logs: list[str] = []


# --- Authorization header ---------------------------------------------------

def test_bearer_is_prefix_then_the_token():
    assert bearer(TOKEN) == f"Bearer {TOKEN}"


@pytest.mark.parametrize("raw", [f"{TOKEN}\n", f"{TOKEN}\r\n", f"  {TOKEN} ", f'"{TOKEN}"', f"'{TOKEN}'\n", f'" {TOKEN} "'])
def test_token_from_an_env_file_is_cleaned(raw):
    assert clean_token(raw) == TOKEN
    assert bearer(raw) == f"Bearer {TOKEN}"


def _sent_authorization(monkeypatch, call) -> str:
    seen = []

    def fake(request, timeout=None):
        seen.append(request.get_header("Authorization"))
        return Reply(["flour"] if "ingredient-list" in request.full_url else {"runs": []})

    monkeypatch.setattr(price_runs.urllib.request, "urlopen", fake)
    monkeypatch.setattr(ingredients.urllib.request, "urlopen", fake)
    call()
    return seen[0]


def test_index_and_ingredient_list_send_the_same_header_with_no_whitespace(monkeypatch):
    dirty = f" {TOKEN}\r\n"
    monkeypatch.setenv("MAILBOX_TOKEN", dirty)
    index_header = _sent_authorization(monkeypatch, lambda: price_runs.fetch_index(RUNS, dirty))
    rebuild_header = _sent_authorization(monkeypatch, lambda: price_runs.fetch_index(RUNS, dirty, rebuild=True))
    post_header = _sent_authorization(monkeypatch, lambda: post_part(RUNS, dirty, b"{}", "part 1"))
    list_header = _sent_authorization(monkeypatch, lambda: ingredients.fetch_ingredient_list(LIST))
    assert index_header == rebuild_header == post_header == list_header == f"Bearer {TOKEN}"
    assert not any(c.isspace() for c in index_header.split(" ", 1)[1])


def test_index_url_forms(monkeypatch):
    urls = []

    def fake(request, timeout=None):
        urls.append((request.get_method(), request.full_url))
        return Reply({"runs": []})

    monkeypatch.setattr(price_runs.urllib.request, "urlopen", fake)
    price_runs.fetch_index(RUNS, TOKEN)
    price_runs.fetch_index(RUNS, TOKEN, rebuild=True)
    assert urls == [("GET", RUNS + "?index"), ("GET", RUNS + "?index&rebuild=1")]


# --- the index parser -------------------------------------------------------

INDEX = {
    "runs": [
        {
            "runId": RUN_ID, "store": "newworld", "scrapedAt": "2026-11-13T08:03:41+13:00", "day": "2026-11-13",
            "parts": 3, "got": [1, 2, 3], "count": 2500, "receivedAt": "2026-11-13T08:10:00Z",
            "complete": True, "source": "pi", "replacedBy": "newworld-2026-11-13-aaaaaaaaaa",
        },
        {"runId": "paknsave-2026-11-08-bbbbbbbbbb", "store": "paknsave", "parts": 3, "got": [1, 3], "complete": False, "source": "pi"},
        {"runId": "woolworths-2026-11-11-cccccccccc", "complete": True, "source": "mailbox", "replacedBy": None},
    ],
    "migrated": True,
    "now": "2026-11-14T00:00:00Z",
}


def test_parser_reads_a_complete_run_with_replaced_by():
    run = parse_index(INDEX)[RUN_ID]
    assert run.complete is True
    assert run.replaced_by == "newworld-2026-11-13-aaaaaaaaaa"
    assert (run.store, run.parts, run.got, run.count, run.source) == ("newworld", 3, [1, 2, 3], 2500, "pi")


def test_parser_reads_an_incomplete_run():
    run = parse_index(INDEX)["paknsave-2026-11-08-bbbbbbbbbb"]
    assert run.complete is False
    assert run.got == [1, 3] and run.parts == 3
    assert run.replaced_by is None


def test_parser_tolerates_missing_optional_fields():
    run = parse_index(INDEX)["woolworths-2026-11-11-cccccccccc"]
    assert run.complete is True and run.replaced_by is None and run.source == "mailbox"
    assert run.parts is None and run.got == []


def test_parser_only_counts_complete_when_it_is_exactly_true():
    runs = parse_index({"runs": [{"runId": "a", "complete": "yes"}, {"runId": "b", "complete": 1}]})
    assert not runs["a"].complete and not runs["b"].complete


@pytest.mark.parametrize("bad", [None, [], "x", {}, {"runs": "no"}, {"runs": {}}])
def test_parser_rejects_a_reply_with_no_runs_list(bad):
    with pytest.raises(ValueError):
        parse_index(bad)


def test_parser_skips_entries_without_a_run_id():
    assert list(parse_index({"runs": [{"complete": True}, "junk", {"runId": "ok"}]})) == ["ok"]


def _serve_index(monkeypatch, body_for):
    calls = []

    def fake(request, timeout=None):
        calls.append(request.full_url.split("api/price-runs")[1])
        return Reply(body_for(len(calls), request.full_url))

    monkeypatch.setattr(price_runs.urllib.request, "urlopen", fake)
    return calls


def test_verify_complete_run_does_not_rebuild(monkeypatch, sleeps):
    calls = _serve_index(monkeypatch, lambda n, url: INDEX)
    check, rebuilt = verify_in_index(RUNS, TOKEN, RUN_ID)
    assert (check.state, rebuilt) == ("complete", False)
    assert check.replaced_by == "newworld-2026-11-13-aaaaaaaaaa"
    assert calls == ["?index"]


def test_verify_incomplete_run_rebuilds_once_then_checks_again(monkeypatch, sleeps):
    incomplete = {"runs": [{"runId": RUN_ID, "complete": False}]}
    calls = _serve_index(monkeypatch, lambda n, url: INDEX if "rebuild" in url or n == 3 else incomplete)
    check, rebuilt = verify_in_index(RUNS, TOKEN, RUN_ID)
    assert (check.state, rebuilt) == ("complete", True)
    assert calls == ["?index", "?index&rebuild=1", "?index"]


def test_verify_run_not_listed_is_incomplete(monkeypatch, sleeps):
    _serve_index(monkeypatch, lambda n, url: {"runs": []})
    check, rebuilt = verify_in_index(RUNS, TOKEN, RUN_ID)
    assert (check.state, rebuilt, check.run) == ("incomplete", True, None)


def test_unreadable_index_is_unknown(monkeypatch, sleeps):
    _serve_index(monkeypatch, lambda n, url: {"nope": 1})
    assert price_runs.check_index(RUNS, TOKEN, RUN_ID).state == "unknown"


# --- Retry-After --------------------------------------------------------------

def test_parse_retry_after_seconds_and_dates():
    assert parse_retry_after("7") == 7
    assert parse_retry_after(" 2.5 ") == 2.5
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # long past
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None


def test_503_with_retry_after_waits_that_long_and_uses_the_same_attempts(monkeypatch, sleeps):
    outcomes = [http_error(503, {"Retry-After": "7"}), http_error(503, {"Retry-After": "9"}), None]
    seen = []

    def fake(request, timeout=None):
        seen.append(1)
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome
        return Reply({"ok": True})

    monkeypatch.setattr(price_runs.urllib.request, "urlopen", fake)
    assert post_part(RUNS, TOKEN, b"{}", "part 1") is True
    assert sleeps == [7, 9]
    assert len(seen) == price_runs.ATTEMPTS == 3


def test_503_keeps_the_attempt_limit_even_with_retry_after(monkeypatch, sleeps):
    count = []

    def fake(request, timeout=None):
        count.append(1)
        raise http_error(503, {"Retry-After": "4"})

    monkeypatch.setattr(price_runs.urllib.request, "urlopen", fake)
    assert post_part(RUNS, TOKEN, b"{}", "part 1") is False
    assert len(count) == price_runs.ATTEMPTS
    assert sleeps == [4, 4]


def test_a_very_long_retry_after_is_capped(monkeypatch, sleeps):
    outcomes = [http_error(503, {"Retry-After": "86400"}), None]

    def fake(request, timeout=None):
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome
        return Reply()

    monkeypatch.setattr(price_runs.urllib.request, "urlopen", fake)
    assert post_part(RUNS, TOKEN, b"{}", "part 1") is True
    assert sleeps == [price_runs.MAX_RETRY_AFTER_SECONDS]


def test_503_without_retry_after_uses_the_usual_delay(monkeypatch, sleeps):
    outcomes = [http_error(503), None]

    def fake(request, timeout=None):
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome
        return Reply()

    monkeypatch.setattr(price_runs.urllib.request, "urlopen", fake)
    assert post_part(RUNS, TOKEN, b"{}", "part 1") is True
    assert sleeps == [price_runs.RETRY_DELAYS_SECONDS[0]]


def test_a_400_is_not_retried(monkeypatch, sleeps):
    count = []

    def fake(request, timeout=None):
        count.append(1)
        raise http_error(400, {"Retry-After": "5"})

    monkeypatch.setattr(price_runs.urllib.request, "urlopen", fake)
    assert post_part(RUNS, TOKEN, b"{}", "part 1") is False
    assert len(count) == 1 and sleeps == []


# --- a 401 --------------------------------------------------------------------

def test_a_401_logs_the_headers_the_app_received_and_never_the_token(monkeypatch, sleeps):
    body = json.dumps({"error": "unauthorized", "receivedHeaders": ["accept", "host", "authorization"], "echo": TOKEN}).encode()

    def fake(request, timeout=None):
        raise http_error(401, body=body)

    monkeypatch.setattr(price_runs.urllib.request, "urlopen", fake)
    assert price_runs.fetch_index(RUNS, TOKEN, rebuild=True) is None
    text = "\n".join(logs)
    assert "status 401" in text
    assert "receivedHeaders" in text and "authorization" in text
    assert TOKEN not in text
    assert sleeps == []  # a 401 is not retried
