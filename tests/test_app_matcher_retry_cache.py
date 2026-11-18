"""Retries, the per-run cache, the rules version and the fallback note in the SUMMARY.
The endpoint is mocked; no test touches the network."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import scraper.app_matcher as app_matcher
from scraper.app_matcher import AppMatchError, match_terms
from scraper.cooking_wine import excluded_reason
from scraper.ingredients import fetch_store_lists
from scraper.search_step import SearchOutcome, category_words, search_products_for_terms, summary_line
from tests.test_app_matcher import MAILBOX, TOKEN, FakeApp, _http_error, _install, _p, configured  # noqa: F401


def _answers(found_by_term):
    def reply(body):
        return [
            {"term": t, "found": t in found_by_term, "productIds": found_by_term.get(t, [])} for t in body["terms"]
        ]

    return reply


class FlakyApp(FakeApp):
    """Fails with `errors` in order, then answers normally."""

    def __init__(self, errors, **kw):
        super().__init__(**kw)
        self._errors = list(errors)

    def __call__(self, request, timeout=None):
        if self._errors:
            self.requests.append((request, timeout))
            raise self._errors.pop(0)
        return super().__call__(request, timeout)


@pytest.fixture
def sleeps(monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(app_matcher.time, "sleep", seen.append)
    return seen


# --- retries ---------------------------------------------------------------------------------

def test_a_transient_failure_is_retried_and_the_run_still_uses_the_app(configured, monkeypatch, sleeps):
    app = _install(monkeypatch, FlakyApp([_http_error(503, "Unavailable"), TimeoutError()], reply=_answers({"garlic": ["P1"]})))
    result = match_terms("paknsave", ["garlic"], [_p("P1", "Garlic")])
    assert result.results["garlic"].found
    assert len(app.requests) == 3 and sleeps == [2.0, 6.0]


def test_retry_after_is_honoured_and_capped(configured, monkeypatch, sleeps):
    slow = urllib.error.HTTPError(MAILBOX, 429, "Too Many", {"Retry-After": "7"}, None)
    huge = urllib.error.HTTPError(MAILBOX, 429, "Too Many", {"Retry-After": "9999"}, None)
    _install(monkeypatch, FlakyApp([slow, huge], reply=_answers({})))
    match_terms("paknsave", ["garlic"], [_p("P1", "Garlic")])
    assert sleeps == [7.0, 30.0]


@pytest.mark.parametrize("error", [_http_error(401, "Unauthorized"), _http_error(400, "Bad Request"), _http_error(404, "Not Found")])
def test_client_errors_are_not_retried(configured, monkeypatch, sleeps, error):
    app = _install(monkeypatch, FakeApp(error=error))
    with pytest.raises(AppMatchError):
        match_terms("paknsave", ["garlic"], [_p("P1", "Garlic")])
    assert len(app.requests) == 1 and sleeps == []


def test_giving_up_names_the_last_reason_and_never_the_token(configured, monkeypatch, sleeps):
    _install(monkeypatch, FakeApp(error=_http_error(500, "Server Error")))
    with pytest.raises(AppMatchError) as caught:
        match_terms("paknsave", ["garlic"], [_p("P1", "Garlic")])
    assert str(caught.value) == "status 500 (Server Error) after 3 tries"
    assert TOKEN not in str(caught.value)


# --- the per-run cache -----------------------------------------------------------------------

def test_the_same_question_twice_makes_one_call(configured, monkeypatch):
    app = _install(monkeypatch, FakeApp(_answers({"garlic": ["P1"]})))
    products = [_p("P1", "Garlic"), _p("P2", "Milk")]
    first = match_terms("paknsave", ["garlic", "saffron"], products)
    second = match_terms("paknsave", ["saffron", "garlic"], list(reversed(products)))
    assert len(app.calls) == 1
    assert second.results == first.results and second.version == first.version


def test_only_terms_not_yet_asked_are_sent(configured, monkeypatch):
    app = _install(monkeypatch, FakeApp(_answers({"garlic": ["P1"], "milk": ["P2"]})))
    products = [_p("P1", "Garlic"), _p("P2", "Milk")]
    match_terms("paknsave", ["garlic"], products)
    result = match_terms("paknsave", ["garlic", "milk"], products)
    assert [c["terms"] for c in app.calls] == [["garlic"], ["milk"]]
    assert result.results["garlic"].found and result.results["milk"].found


def test_other_products_or_another_store_are_not_served_from_the_cache(configured, monkeypatch):
    app = _install(monkeypatch, FakeApp(_answers({})))
    match_terms("paknsave", ["garlic"], [_p("P1", "Garlic")])
    match_terms("paknsave", ["garlic"], [_p("P1", "Garlic"), _p("P2", "Garlic Bulb")])
    match_terms("newworld", ["garlic"], [_p("P1", "Garlic")])
    match_terms("paknsave", ["garlic"], [_p("P1", "Garlic Powder")])
    assert len(app.calls) == 4


def test_a_failed_call_is_not_cached(configured, monkeypatch):
    _install(monkeypatch, FakeApp(error=_http_error(401, "Unauthorized")))
    with pytest.raises(AppMatchError):
        match_terms("paknsave", ["garlic"], [_p("P1", "Garlic")])
    app = _install(monkeypatch, FakeApp(_answers({"garlic": ["P1"]})))
    assert match_terms("paknsave", ["garlic"], [_p("P1", "Garlic")]).results["garlic"].found
    assert len(app.calls) == 1


def test_a_new_rules_version_drops_the_cache_and_asks_everything_again(configured, monkeypatch):
    products = [_p("P1", "Garlic"), _p("P2", "Milk")]
    first = _install(monkeypatch, FakeApp(_answers({"garlic": ["P1"]}), version="v1"))
    match_terms("paknsave", ["garlic"], products)
    second = _install(monkeypatch, FakeApp(_answers({"garlic": ["P1"], "milk": ["P2"]}), version="v2"))
    result = match_terms("paknsave", ["garlic", "milk"], products)
    assert [c["terms"] for c in second.calls] == [["milk"], ["garlic", "milk"]]
    assert result.version == "v2" and len(first.calls) == 1


# --- matchRules version ----------------------------------------------------------------------

class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_lists_carry_the_match_rules_version(monkeypatch):
    monkeypatch.setattr(
        "scraper.ingredients.urllib.request.urlopen",
        lambda r, timeout=None: _Resp(json.dumps({"search": ["garlic"], "matchRules": {"version": "b1a77945", "synonyms": {}}}).encode()),
    )
    assert fetch_store_lists("paknsave").match_rules_version == "b1a77945"
    monkeypatch.setattr("scraper.ingredients.urllib.request.urlopen", lambda r, timeout=None: _Resp(b'{"search": []}'))
    assert fetch_store_lists("paknsave").match_rules_version is None


def test_a_rules_version_change_during_the_run_is_logged_once_and_the_new_answer_used(configured, monkeypatch, capsys):
    _install(monkeypatch, FakeApp(_answers({"garlic": ["P1"]}), version="new"))
    outcome = search_products_for_terms("paknsave", ["garlic"], [_p("P1", "Garlic")], set(), rules_version="old")
    out = capsys.readouterr().out
    assert out.count("matchRules version changed during the run (old -> new)") == 1
    assert outcome.matcher == "app new" and list(outcome.found) == ["garlic"]


def test_an_unchanged_rules_version_logs_nothing(configured, monkeypatch, capsys):
    _install(monkeypatch, FakeApp(_answers({}), version="same"))
    search_products_for_terms("paknsave", ["garlic"], [_p("P1", "Garlic")], set(), rules_version="same")
    assert "matchRules" not in capsys.readouterr().out


# --- the endpoint decides; the Python rules do not -------------------------------------------

def test_the_apps_answer_beats_the_local_rules_both_ways(configured, monkeypatch):
    # Locally "pickles" does not match a gherkins jar and "cream" does match cream of tartar.
    products = [_p("P1", "Mt Olive Whole Dill Gherkins 500g"), _p("P2", "Cream of Tartar 50g")]
    _install(monkeypatch, FakeApp(_answers({"pickles": ["P1"]})))
    outcome = search_products_for_terms("paknsave", ["pickles", "cream"], products, category_words("pickles", ("cream",)))
    assert [p["product_id"] for p in outcome.found["pickles"]] == ["P1"]
    assert outcome.not_found == ["cream"]


# --- the fallback note in the SUMMARY --------------------------------------------------------

def test_the_summary_says_why_the_local_matcher_was_used(configured, monkeypatch, capsys, sleeps):
    _install(monkeypatch, FakeApp(error=_http_error(500, "Server Error")))
    outcome = search_products_for_terms("paknsave", ["garlic"], [_p("P1", "Garlic")], set())
    line = summary_line(3, outcome, 1, 5)
    assert "matcher local fallback, found via fallback key 0, app matcher unavailable (status 500 (Server Error) after 3 tries), run time 0m05s" in line
    assert capsys.readouterr().out.count("App matcher unavailable") == 1


def test_the_summary_has_no_fallback_note_when_the_app_answered():
    assert "unavailable" not in summary_line(0, SearchOutcome(searched=["a"], matcher="app v1"), 1, 5)


# --- scraping-only rules are untouched ---------------------------------------------------------

def test_not_found_still_needs_a_scraped_category_when_the_app_answers(configured, monkeypatch):
    _install(monkeypatch, FakeApp(_answers({})))
    outcome = search_products_for_terms("paknsave", ["butter", "saffron"], [_p("P1", "Milk")], category_words("butter", ()))
    assert outcome.not_found == ["butter"] and outcome.unreported == ["saffron"]


def test_cooking_wine_shelf_words_still_apply():
    assert excluded_reason("Brancott Sparkling Sauvignon Blanc") == "sparkling"
    assert excluded_reason("Selaks Sauvignon Blanc") is None


# --- compare_matchers ------------------------------------------------------------------------

def test_pair_differences_and_a_spread_of_examples():
    from scraper.compare_matchers import latest_snapshot, pair_differences, spread

    by_id = {"1": {"name": "Dill Gherkins"}, "2": {"name": "Fresh Dill"}, "3": {"name": "Cream of Tartar"}}
    terms = ["gherkins", "dill", "cream"]
    app = {"gherkins": {"1"}, "dill": {"2"}, "cream": set()}
    local = {"gherkins": {"1"}, "dill": {"1", "2"}, "cream": {"3"}}
    diffs = pair_differences(terms, app, local, by_id, {"gherkins": "pickles"})
    assert [(d["product"], d["term"], d["local"], d["app"]) for d in diffs] == [
        ("Dill Gherkins", "dill", "match", "no match"),
        ("Cream of Tartar", "cream", "match", "no match"),
    ]
    many = [{"term": "a", "n": i} for i in range(5)] + [{"term": "b", "n": 0}]
    assert [(d["term"], d["n"]) for d in spread(many, 3)] == [("a", 0), ("b", 0), ("a", 1)]
    assert latest_snapshot("paknsave", data_dir=__import__("pathlib").Path("/nonexistent")) is None
