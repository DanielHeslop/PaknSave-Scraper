"""The search step asks the app's /api/match-terms for found / not-found and falls
back to the local matcher when that call fails. The endpoint is mocked here; no
test touches the network."""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error

import pytest

import scraper.app_matcher as app_matcher
import scraper.weekly_combined as weekly_combined
from scraper import woolworths
from scraper.app_matcher import AppMatchError, match_terms
from scraper.ingredients import StoreLists
from scraper.price_runs import RunResult
from scraper.search_step import (
    SearchOutcome,
    category_words,
    log_outcome,
    search_products_for_terms,
    summary_line,
)
from scraper.weekly_combined import main_async, parse_args

TOKEN = "s3cret-token-value"
MAILBOX = "https://example-site.netlify.app/api/price-mailbox"


def _p(pid, name, supermarket="Pak'nSave"):
    return {"product_id": pid, "name": name, "supermarket": supermarket}


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self.status = status
        self._buf = io.BytesIO(body)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApp:
    """Stands in for urlopen. `reply` maps the request body to a result list, or raises."""

    def __init__(self, reply=None, version="2026-11-10", error=None, status=200):
        self.calls: list[dict] = []
        self.requests = []
        self.reply = reply
        self.version = version
        self.error = error
        self.status = status

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        body = json.loads(request.data)
        self.calls.append(body)
        results = self.reply(body) if self.reply else [{"term": t, "found": False} for t in body["terms"]]
        return _Response(json.dumps({"version": self.version, "results": results}).encode(), self.status)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("MAILBOX_URL", MAILBOX)
    monkeypatch.setenv("MAILBOX_TOKEN", TOKEN)


def _install(monkeypatch, app):
    monkeypatch.setattr(app_matcher.urllib.request, "urlopen", app)
    return app


def _http_error(code, reason):
    return urllib.error.HTTPError(MAILBOX, code, reason, {}, None)


def test_normal_reply_and_request_shape(configured, monkeypatch):
    app = _install(monkeypatch, FakeApp(lambda b: [
        {"term": "garlic", "found": True, "productIds": ["P1"]},
        {"term": "saffron", "found": False, "productIds": []},
    ]))
    result = match_terms("newworld", ["garlic", "saffron"], [_p("P1", "Garlic Bulb"), _p("P2", "Milk")])

    assert result.version == "2026-11-10"
    assert result.results["garlic"].found and result.results["garlic"].product_ids == ("P1",)
    assert not result.results["saffron"].found

    (request, timeout), = app.requests
    assert request.full_url == "https://example-site.netlify.app/api/match-terms"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert timeout == 30
    assert app.calls == [{
        "store": "newworld",
        "terms": ["garlic", "saffron"],
        "products": [{"id": "P1", "name": "Garlic Bulb"}, {"id": "P2", "name": "Milk"}],
    }]


def test_outcome_uses_app_results_and_scraped_categories(configured, monkeypatch, capsys):
    _install(monkeypatch, FakeApp(lambda b: [
        {"term": "garlic", "found": True, "productIds": ["P1"]},
        {"term": "onion", "found": False},
        {"term": "saffron", "found": False},
    ]))
    products = [_p("P1", "Garlic Bulb"), _p("P1", "Garlic Bulb")]
    outcome = search_products_for_terms(
        "paknsave", ["garlic", "onion", "saffron"], products, category_words("vegetables", ("onion",))
    )
    assert outcome.matcher == "app 2026-11-10"
    assert [p["product_id"] for p in outcome.found["garlic"]] == ["P1"]
    assert outcome.not_found == ["onion"]  # vegetables was scraped
    assert outcome.unreported == ["saffron"]  # no category to blame
    assert "App matcher unavailable" not in capsys.readouterr().out


def test_chunks_over_300_terms_and_6000_products(configured, monkeypatch):
    def reply(body):
        assert len(body["terms"]) <= 300 and len(body["products"]) <= 6000
        names = {p["id"]: p["name"] for p in body["products"]}
        return [
            {"term": t, "found": t in names.values(), "productIds": [i for i, n in names.items() if n == t]}
            for t in body["terms"]
        ]

    app = _install(monkeypatch, FakeApp(reply))
    terms = [f"item{i}" for i in range(650)]
    products = [_p(f"P{i}", f"item{i}") for i in range(650)] + [_p(f"X{i}", f"filler{i}") for i in range(12350)]
    result = match_terms("paknsave", terms, products)

    assert len(app.calls) == 3 * 3  # ceil(650/300) terms chunks x ceil(13000/6000) product chunks
    assert len(result.results) == 650
    assert all(r.found and r.product_ids == (f"P{i}",) for i, r in enumerate(result.results[t] for t in terms))
    assert result.version == "2026-11-10"


def test_chunk_merge_prefers_direct_matches_over_via(configured, monkeypatch):
    def reply(body):
        if body["products"][0]["id"] == "A":
            return [{"term": "tamari", "found": True, "productIds": ["A"], "via": "soy sauce"}]
        return [{"term": "tamari", "found": True, "productIds": ["B"]}]

    _install(monkeypatch, FakeApp(reply))
    result = match_terms("paknsave", ["tamari"], [_p("A", "Soy Sauce"), _p("B", "Tamari")], max_products=1)
    assert result.results["tamari"].product_ids == ("B",) and result.results["tamari"].via is None

    _install(monkeypatch, FakeApp(lambda b: [{"term": "tamari", "found": True, "productIds": [b["products"][0]["id"]], "via": "soy sauce"}]))
    result = match_terms("paknsave", ["tamari"], [_p("A", "Soy Sauce"), _p("B", "Soy Sauce 2")], max_products=1)
    assert result.results["tamari"].product_ids == ("A", "B") and result.results["tamari"].via == "soy sauce"


@pytest.mark.parametrize(
    "error, expected",
    [
        (_http_error(401, "Unauthorized"), "status 401"),
        (_http_error(500, "Internal Server Error"), "status 500"),
        (TimeoutError(), "timed out after 30s"),
        (urllib.error.URLError(TimeoutError()), "timed out after 30s"),
        (urllib.error.URLError("no route"), "could not connect"),
    ],
)
def test_failures_fall_back_to_the_local_matcher(configured, monkeypatch, capsys, error, expected):
    app = _install(monkeypatch, FakeApp(error=error))
    products = [_p("P1", "Garlic Bulb")]
    outcome = search_products_for_terms("paknsave", ["garlic", "saffron"], products, category_words("veg", ("saffron",)))

    out = capsys.readouterr().out
    assert len(app.requests) == 1
    assert outcome.matcher == "local fallback"
    assert list(outcome.found) == ["garlic"] and outcome.not_found == ["saffron"]
    lines = [line for line in out.splitlines() if "App matcher unavailable" in line]
    assert len(lines) == 1 and expected in lines[0]
    assert TOKEN not in out


def test_slow_reply_over_the_overall_limit_times_out(configured, monkeypatch):
    clock = iter([0.0, 31.0, 31.0, 31.0])
    monkeypatch.setattr(app_matcher.time, "monotonic", lambda: next(clock))
    _install(monkeypatch, FakeApp())
    with pytest.raises(AppMatchError, match="timed out after 30s"):
        match_terms("paknsave", ["garlic"], [_p("P1", "Garlic")])


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"version": "v1"}', b'{"version": "v1", "results": [{"term": "garlic"}]}',
     b'{"version": "v1", "results": []}'],
)
def test_malformed_replies_fall_back(configured, monkeypatch, capsys, body):
    monkeypatch.setattr(app_matcher.urllib.request, "urlopen", lambda r, timeout=None: _Response(body))
    outcome = search_products_for_terms("paknsave", ["garlic"], [_p("P1", "Garlic")], set())
    assert outcome.matcher == "local fallback" and list(outcome.found) == ["garlic"]
    assert "App matcher unavailable" in capsys.readouterr().out


def test_unconfigured_env_falls_back_without_a_call(monkeypatch, capsys):
    app = _install(monkeypatch, FakeApp())
    outcome = search_products_for_terms("paknsave", ["garlic"], [_p("P1", "Garlic")], set())
    assert app.requests == [] and outcome.matcher == "local fallback"
    assert "not set" in capsys.readouterr().out


def test_via_is_logged_and_counted(configured, monkeypatch, capsys):
    _install(monkeypatch, FakeApp(lambda b: [
        {"term": "tamari", "found": True, "productIds": ["P1"], "via": "soy sauce"},
        {"term": "garlic", "found": True, "productIds": ["P2"]},
    ]))
    outcome = search_products_for_terms("paknsave", ["tamari", "garlic"], [_p("P1", "Soy Sauce"), _p("P2", "Garlic")], set())
    log_outcome(outcome)
    assert "tamari: found via soy sauce" in capsys.readouterr().out
    line = summary_line(5, outcome, 3, 61)
    assert line.endswith("matcher app 2026-11-10, found via fallback key 1, run time 1m01s")


def test_summary_names_the_local_fallback():
    outcome = SearchOutcome(searched=["a"], matcher="local fallback")
    assert "matcher local fallback, found via fallback key 0" in summary_line(0, outcome, 1, 5)


# --- through the weekly job -------------------------------------------------

_CATEGORY_PRODUCTS = [
    {"product_id": "P1", "name": "Carrots", "category": "vegetables", "size": "kg", "price": 1.99,
     "unit_price": 1.99, "unit": "kg", "scraped_at": "2026-11-10", "supermarket": "Pak'nSave"},
]


def _run_weekly(monkeypatch, tmp_path):
    async def fake_scrape(*a, **k):
        return {"products": _CATEGORY_PRODUCTS,
                "stats": {"pages_scraped": 1, "pages_skipped": [], "categories_scraped": 1, "products_found": 1,
                          "products_saved": 1, "products_with_unit_price": 1, "products_without_unit_price": 0,
                          "products_dropped": []},
                "scraped_categories": {"vegetables": ("carrot", "onion")}}

    published, reports = [], []
    monkeypatch.setattr(weekly_combined, "scrape_all", fake_scrape)
    monkeypatch.setattr(weekly_combined, "fetch_store_lists", lambda s, u: StoreLists(
        store="paknsave", search_terms=["tamari", "carrots", "saffron"], watch_terms=["onion"]))
    monkeypatch.setattr(weekly_combined, "publish_run", lambda p, *a, **k: published.append(p) or RunResult(items=len(p)))
    monkeypatch.setattr(weekly_combined, "report_outcome", lambda s, o: reports.append((s, o)))
    args = parse_args(["--save"])
    args.snapshots_dir, args.history_path = tmp_path / "snap", tmp_path / "h.json"
    return asyncio.run(main_async(args)), published, reports


def test_weekly_job_uses_the_app_matcher(configured, monkeypatch, tmp_path, capsys):
    _install(monkeypatch, FakeApp(lambda b: [
        {"term": "tamari", "found": True, "productIds": ["P1"], "via": "carrot"},
        {"term": "carrots", "found": True, "productIds": ["P1"]},
        {"term": "saffron", "found": False},
        {"term": "onion", "found": False},
    ]))
    code, published, reports = _run_weekly(monkeypatch, tmp_path)

    out = capsys.readouterr().out
    (store, outcome), = reports
    assert code == 0 and store == "paknsave"
    assert set(outcome.found) == {"tamari", "carrots"}
    assert outcome.not_found == ["onion"] and outcome.unreported == ["saffron"]
    assert "tamari: found via carrot" in out
    last = out.strip().splitlines()[-1]
    assert "matcher app 2026-11-10, found via fallback key 1" in last
    assert TOKEN not in out


@pytest.mark.parametrize("error", [_http_error(500, "Server Error"), _http_error(401, "Unauthorized"), TimeoutError()])
def test_weekly_job_still_publishes_when_the_endpoint_fails(configured, monkeypatch, tmp_path, capsys, error):
    _install(monkeypatch, FakeApp(error=error))
    code, published, reports = _run_weekly(monkeypatch, tmp_path)

    out = capsys.readouterr().out
    assert code == 0 and published == [_CATEGORY_PRODUCTS]
    (_, outcome), = reports
    assert set(outcome.found) == {"carrots"} and outcome.not_found == ["onion"]
    assert "matcher local fallback, found via fallback key 0" in out.strip().splitlines()[-1]


# --- Woolworths ---------------------------------------------------------------

def _wp(pid, name):
    return {"product_id": pid, "name": name, "supermarket": "Woolworths"}


def _woolworths_outcome():
    return SearchOutcome(searched=["rice", "tamari", "wine"], found={"rice": [_wp("1", "Rice")]},
                         not_found=["tamari"], unreported=["wine"], skipped=["x"])


def test_woolworths_app_matcher_keeps_unreported_for_failed_searches(configured, monkeypatch):
    _install(monkeypatch, FakeApp(lambda b: [
        {"term": "rice", "found": True, "productIds": ["1"]},
        {"term": "tamari", "found": True, "productIds": ["2"], "via": "soy sauce"},
        {"term": "wine", "found": False},
    ]))
    outcome = woolworths.apply_app_matcher(_woolworths_outcome(), [_wp("1", "Rice"), _wp("2", "Soy Sauce")])
    assert set(outcome.found) == {"rice", "tamari"} and outcome.via == {"tamari": "soy sauce"}
    assert outcome.not_found == [] and outcome.unreported == ["wine"]
    assert outcome.skipped == ["x"] and outcome.matcher == "app 2026-11-10"


def test_woolworths_falls_back_to_the_per_term_local_outcome(configured, monkeypatch, capsys):
    _install(monkeypatch, FakeApp(error=_http_error(500, "Server Error")))
    outcome = woolworths.apply_app_matcher(_woolworths_outcome(), [_wp("1", "Rice")])
    assert list(outcome.found) == ["rice"] and outcome.not_found == ["tamari"]
    assert outcome.matcher == "local fallback"
    assert "App matcher unavailable" in capsys.readouterr().out
