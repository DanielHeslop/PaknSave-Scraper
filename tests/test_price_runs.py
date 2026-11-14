"""Tests for posting a scrape run to /api/price-runs (scraper/price_runs.py).

The app is replaced by FakeApp, a stand-in for urllib's urlopen. Nothing here
touches the network.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
from datetime import datetime

import pytest

import scraper.price_runs as price_runs
import scraper.post_run as post_run
from scraper.price_runs import (
    MAX_ITEMS_PER_PART,
    MAX_PART_BYTES,
    RunResult,
    make_run_id,
    plan_run,
    publish_run,
    split_items,
)

TOKEN = "secret-token-xyz"
MAILBOX = "https://app.example/api/price-mailbox"
RUNS = "https://app.example/api/price-runs"
SCRAPED_AT = "2026-11-13T08:03:41+13:00"


def product(i: int, supermarket: str = "New World", name: str | None = None) -> dict:
    return {
        "product_id": f"P{i:07d}",
        "name": name or f"Product {i}",
        "category": "x",
        "size": "500g",
        "price": 2.0 + (i % 50) / 10,
        "unit_price": 4.0,
        "unit": "kg",
        "scraped_at": "2026-11-13",
        "supermarket": supermarket,
    }


def items(n: int) -> list[dict]:
    return price_runs.build_payload_items([product(i) for i in range(n)])


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None):
        self.status = status
        self._body = json.dumps(body or {}).encode()

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeApp:
    """Stands in for urlopen: stores parts by (runId, part) and answers the run list."""

    def __init__(self):
        self.calls: list[tuple[str, str, int | None]] = []  # (method, path+query, part)
        self.requests: list = []
        self.parts: dict[tuple[str, int], dict] = {}
        self.fail: dict[int, list] = {}  # part number -> outcomes to use in order ("500", "timeout", "400", "net")
        self.needs_rebuild = False
        self.rebuilt = False
        self.in_flight = 0
        self.max_in_flight = 0
        self.index_status = 200

    def _complete(self, run_id: str) -> bool:
        if self.needs_rebuild and not self.rebuilt:
            return False
        have = {p for (r, p) in self.parts if r == run_id}
        total = next(v["parts"] for (r, _), v in self.parts.items() if r == run_id)
        return have == set(range(1, total + 1))

    def __call__(self, request, timeout=None):
        url = request.full_url
        path = url.split("app.example", 1)[1]
        method = request.get_method()
        self.requests.append(request)
        if path.startswith("/api/scrape-exclusions"):
            return FakeResponse(200, {"excluded": []})
        assert path.startswith("/api/price-runs"), path
        if method == "POST":
            body = json.loads(request.data)
            part = body["part"]
            self.calls.append(("POST", path, part))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                outcomes = self.fail.get(part)
                if outcomes:
                    outcome = outcomes.pop(0)
                    if outcome == "timeout":
                        raise TimeoutError
                    if outcome == "net":
                        raise urllib.error.URLError("connection reset")
                    if outcome != "ok":
                        code = int(outcome)
                        raise urllib.error.HTTPError(url, code, "Boom", {}, io.BytesIO(b"{}"))
                self.parts[(body["runId"], part)] = body
            finally:
                self.in_flight -= 1
            return FakeResponse(200, {"ok": True})
        self.calls.append(("GET", path, None))
        if self.index_status != 200:
            raise urllib.error.HTTPError(url, self.index_status, "Nope", {}, io.BytesIO(b"{}"))
        if "rebuild=1" in path:
            self.rebuilt = True
        run_ids = sorted({r for (r, _) in self.parts})
        return FakeResponse(200, {"runs": [{"runId": r, "complete": self._complete(r)} for r in run_ids]})


@pytest.fixture
def app(monkeypatch, tmp_path):
    fake = FakeApp()
    monkeypatch.setenv("MAILBOX_URL", MAILBOX)
    monkeypatch.setenv("MAILBOX_TOKEN", TOKEN)
    monkeypatch.setattr(price_runs.urllib.request, "urlopen", fake)
    monkeypatch.setattr(price_runs, "PENDING_DIR", str(tmp_path / "pending"))
    sleeps = []
    monkeypatch.setattr(price_runs, "_sleep", sleeps.append)
    fake.sleeps = sleeps
    return fake


def run_calls(app: FakeApp) -> list[tuple[str, str, int | None]]:
    return app.calls


# --- parting ---------------------------------------------------------------

def test_parts_hold_at_most_1000_items():
    parts = split_items(items(2500))
    assert [len(p) for p in parts] == [1000, 1000, 500]
    assert [i for p in parts for i in p] == items(2500)


def test_a_small_run_is_one_part():
    assert len(split_items(items(10))) == 1
    assert split_items([]) == []


def test_parts_stay_under_the_byte_limit_even_when_items_are_large():
    big = [dict(i, promo_label="x" * 5000) for i in items(900)]  # ~4.5 MB in total
    plan = plan_run("newworld", SCRAPED_AT, big)
    assert len(plan.parts) >= 3
    for number in range(1, len(plan.parts) + 1):
        assert len(plan.body(number)) <= MAX_PART_BYTES
        assert len(plan.parts[number - 1]) <= MAX_ITEMS_PER_PART
    assert sum(len(p) for p in plan.parts) == 900


def test_item_that_alone_exceeds_the_limit_gets_its_own_part():
    parts = split_items([{"product_id": "a", "pad": "x" * 300}, {"product_id": "b"}], max_bytes=200)
    assert [len(p) for p in parts] == [1, 1]


def test_real_sized_run_fits_few_parts():
    plan = plan_run("newworld", SCRAPED_AT, items(2500))
    assert len(plan.parts) == 3
    assert all(len(plan.body(n)) < 2_000_000 for n in (1, 2, 3))


# --- run id ----------------------------------------------------------------

def test_run_id_is_store_date_and_short_hash():
    run_id = make_run_id("newworld", SCRAPED_AT, items(5))
    store, date, digest = run_id.split("-", 1)[0], run_id[9:19], run_id[20:]
    assert (store, date) == ("newworld", "2026-11-13") and len(digest) == 10


def test_run_id_is_stable_for_the_same_content():
    a = make_run_id("newworld", SCRAPED_AT, items(50))
    assert a == make_run_id("newworld", SCRAPED_AT, list(reversed(items(50))))
    assert a == make_run_id("newworld", "2026-11-13T21:59:00+13:00", items(50))  # time of day does not matter


def test_run_id_changes_with_content_store_or_day():
    base = make_run_id("newworld", SCRAPED_AT, items(50))
    changed = items(50)
    changed[7]["price"] += 0.01
    assert make_run_id("newworld", SCRAPED_AT, changed) != base
    assert make_run_id("newworld", SCRAPED_AT, items(49)) != base
    assert make_run_id("paknsave", SCRAPED_AT, items(50)) != base
    assert make_run_id("newworld", "2026-11-14T08:00:00+13:00", items(50)) != base


def test_parts_are_the_same_whatever_order_the_products_came_in():
    forward = plan_run("newworld", SCRAPED_AT, items(1500))
    backward = plan_run("newworld", SCRAPED_AT, list(reversed(items(1500))))
    assert forward.parts == backward.parts and forward.run_id == backward.run_id


# --- posting with a mocked endpoint ---------------------------------------------

def test_posts_every_part_in_order_then_checks_the_index(app):
    result = publish_run([product(i) for i in range(2500)], "newworld", SCRAPED_AT)
    assert [c for c in app.calls] == [
        ("POST", "/api/price-runs", 1),
        ("POST", "/api/price-runs", 2),
        ("POST", "/api/price-runs", 3),
        ("GET", "/api/price-runs?index", None),
    ]
    assert result.posted and result.items == 2500 and result.index == "complete" and not result.rebuilt
    assert result.summary() == "run posted (parts 3/3, index complete)"


def test_request_shape_and_authorisation(app):
    publish_run([product(i) for i in range(3)], "newworld", SCRAPED_AT)
    post = next(r for r in app.requests if r.get_method() == "POST")
    body = json.loads(post.data)
    assert set(body) == {"runId", "store", "scrapedAt", "part", "parts", "items"}
    assert (body["store"], body["part"], body["parts"], body["scrapedAt"]) == ("newworld", 1, 1, SCRAPED_AT)
    assert body["runId"].startswith("newworld-2026-11-13-")
    assert len(body["items"]) == 3 and body["items"][0]["supermarket"] == "New World"
    assert post.get_header("Authorization") == f"Bearer {TOKEN}"
    assert post.get_header("Content-type") == "application/json"
    assert all(r.get_header("Authorization") == f"Bearer {TOKEN}" for r in app.requests if "price-runs" in r.full_url)


def test_parts_are_never_sent_in_parallel(app):
    publish_run([product(i) for i in range(3500)], "newworld", SCRAPED_AT)
    assert app.max_in_flight == 1
    posted = [p for m, _, p in app.calls if m == "POST"]
    assert posted == [1, 2, 3, 4]


def test_a_failed_part_is_retried_and_the_run_still_completes(app):
    app.fail[2] = ["500", "timeout"]
    result = publish_run([product(i) for i in range(2500)], "newworld", SCRAPED_AT)
    posted = [p for m, _, p in app.calls if m == "POST"]
    assert posted == [1, 2, 2, 2, 3]  # part 2 took three attempts, still in order
    assert result.posted and result.index == "complete"
    assert app.sleeps  # waited between attempts


def test_a_part_that_keeps_failing_is_reported_missing_and_the_rest_still_go(app):
    app.fail[2] = ["500", "500", "500"]
    result = publish_run([product(i) for i in range(2500)], "newworld", SCRAPED_AT)
    assert [p for m, _, p in app.calls if m == "POST"] == [1, 2, 2, 2, 3]
    assert result.missing == [2] and not result.posted
    assert result.items == 1500
    assert result.summary() == "run failed (missing parts 2)"
    assert not any(m == "GET" for m, _, _ in app.calls)  # no index check for a run with missing parts


def test_a_rejected_part_is_not_retried(app):
    app.fail[1] = ["400"]
    result = publish_run([product(i) for i in range(5)], "newworld", SCRAPED_AT)
    assert [p for m, _, p in app.calls if m == "POST"] == [1]
    assert result.missing == [1]


def test_connection_errors_are_retried(app):
    app.fail[1] = ["net", "net"]
    result = publish_run([product(i) for i in range(5)], "newworld", SCRAPED_AT)
    assert result.posted and [p for m, _, p in app.calls if m == "POST"] == [1, 1, 1]


def test_posting_the_same_run_again_replaces_its_parts(app):
    products = [product(i) for i in range(2500)]
    first = publish_run(products, "newworld", SCRAPED_AT)
    second = publish_run(list(reversed(products)), "newworld", SCRAPED_AT)
    assert first.run_id == second.run_id
    assert len(app.parts) == 3  # same (runId, part) keys, nothing added
    assert second.posted and second.index == "complete"


def test_rebuild_is_asked_for_once_when_the_run_is_not_complete(app):
    app.needs_rebuild = True
    result = publish_run([product(i) for i in range(1500)], "newworld", SCRAPED_AT)
    assert [(m, path) for m, path, _ in app.calls if m == "GET"] == [
        ("GET", "/api/price-runs?index"),
        ("GET", "/api/price-runs?index&rebuild=1"),
        ("GET", "/api/price-runs?index"),
    ]
    assert result.rebuilt and result.index == "complete"
    assert result.summary() == "run posted (parts 2/2, index complete)"


def test_no_rebuild_when_the_index_already_shows_complete(app):
    publish_run([product(i) for i in range(5)], "newworld", SCRAPED_AT)
    assert not any("rebuild" in path for _, path, _ in app.calls)


def test_a_run_still_incomplete_after_one_rebuild_is_said_so(app, monkeypatch):
    monkeypatch.setattr(FakeApp, "_complete", lambda self, run_id: False)
    result = publish_run([product(i) for i in range(5)], "newworld", SCRAPED_AT)
    assert [path for m, path, _ in app.calls if m == "GET"].count("/api/price-runs?index&rebuild=1") == 1
    assert result.index == "incomplete"
    assert result.summary() == "run posted (parts 1/1, index NOT complete)"


def test_an_unreadable_index_does_not_fail_the_run(app):
    app.index_status = 403
    result = publish_run([product(i) for i in range(5)], "newworld", SCRAPED_AT)
    assert result.posted and result.index == "unknown"
    assert "index could not be checked" in result.summary()


def test_scraped_at_is_the_scrape_time_not_the_post_time(app):
    publish_run([product(1)], "newworld", "2026-11-06T08:11:09+13:00")
    body = json.loads(next(r for r in app.requests if r.get_method() == "POST").data)
    assert body["scrapedAt"] == "2026-11-06T08:11:09+13:00"
    assert body["runId"].startswith("newworld-2026-11-06-")


def test_exclusions_still_filter_the_items(app, monkeypatch):
    monkeypatch.setattr(price_runs, "_fetch_excluded_keys", lambda url, token: {"New World|P0000001"})
    publish_run([product(i) for i in range(3)], "newworld", SCRAPED_AT)
    body = json.loads(next(r for r in app.requests if r.get_method() == "POST").data)
    assert [i["product_id"] for i in body["items"]] == ["P0000000", "P0000002"]


def test_the_token_is_never_printed(app, capsys):
    app.fail[1] = ["500", "500", "500"]
    publish_run([product(1)], "newworld", SCRAPED_AT)
    assert TOKEN not in capsys.readouterr().out


def test_not_configured_skips_quietly(monkeypatch, capsys):
    monkeypatch.delenv("MAILBOX_URL", raising=False)
    result = publish_run([product(1)], "newworld", SCRAPED_AT)
    assert result.items == 0 and "skipped" in capsys.readouterr().out
    assert result.summary() == "run not posted (MAILBOX_URL/MAILBOX_TOKEN not set)"


def test_price_runs_url_override(monkeypatch):
    monkeypatch.setenv("PRICE_RUNS_URL", "https://other.example/x")
    assert price_runs.price_runs_url(MAILBOX) == "https://other.example/x"


# --- dry run ------------------------------------------------------------------

def test_dry_run_posts_nothing_and_reports_parts_and_bytes(app, capsys):
    result = publish_run([product(i) for i in range(2500)], "newworld", SCRAPED_AT, dry_run=True)
    out = capsys.readouterr().out
    assert not [c for c in app.calls]
    assert result.run_id in out and "2500 item(s) in 3 part(s)" in out
    assert "part 1/3: 1000 item(s)" in out and "part 3/3: 500 item(s)" in out and " bytes" in out


def test_dry_run_works_without_the_app_configured(monkeypatch, capsys):
    monkeypatch.delenv("MAILBOX_URL", raising=False)
    result = publish_run([product(i) for i in range(5)], "newworld", SCRAPED_AT, dry_run=True)
    assert result.run_id and "Dry run" in capsys.readouterr().out


# --- the legacy mailbox ------------------------------------------------------------

def test_legacy_flag_posts_to_the_old_mailbox_only(app, monkeypatch):
    sent = []
    monkeypatch.setattr(price_runs, "publish_to_mailbox", lambda products, dry_run=False: sent.append(products) or len(products))
    result = publish_run([product(i) for i in range(4)], "newworld", SCRAPED_AT, legacy=True)
    assert len(sent) == 1 and result.items == 4
    assert app.calls == []  # nothing went to /api/price-runs
    assert result.summary() == "run not posted (legacy mailbox)"


def test_without_the_flag_the_old_mailbox_is_not_used(app, monkeypatch):
    monkeypatch.setattr(price_runs, "publish_to_mailbox", lambda *a, **k: pytest.fail("old mailbox used"))
    assert publish_run([product(1)], "newworld", SCRAPED_AT).posted


@pytest.mark.parametrize("module", ["scraper.weekly_combined", "scraper.woolworths", "scraper.run", "scraper.post_run"])
def test_every_entry_point_has_the_legacy_flag(module):
    import importlib

    mod = importlib.import_module(module)
    argv = ["--site", "newworld"] if module.endswith("post_run") else []
    assert mod.parse_args(argv + ["--legacy-mailbox"]).legacy_mailbox is True
    assert mod.parse_args(argv).legacy_mailbox is False


# --- missing parts are retried later ------------------------------------------------

def test_missing_parts_are_retried_at_the_next_run(app, tmp_path):
    snapshot = tmp_path / "2026-11-13.json"
    products = [product(i) for i in range(2500)]
    snapshot.write_text(json.dumps(products))
    app.fail[2] = ["500", "500", "500"]
    first = publish_run(products, "newworld", SCRAPED_AT, snapshot_path=snapshot)
    assert first.missing == [2]
    pending = json.loads(open(os.path.join(price_runs.PENDING_DIR, "newworld.json")).read())
    assert pending[first.run_id]["missing"] == [2]

    app.calls.clear()
    nxt = [product(i, name="other") for i in range(10)]
    publish_run(nxt, "newworld", "2026-11-20T08:00:00+13:00")
    posted = [(m, p) for m, _, p in app.calls if m == "POST"]
    assert posted[0] == ("POST", 2)  # the earlier run's missing part goes first
    assert len(app.parts) == 3 + 1  # the old run is whole, plus the new run's one part
    assert not os.path.exists(os.path.join(price_runs.PENDING_DIR, "newworld.json"))


def test_post_run_resends_a_saved_snapshot_and_reuses_the_run_id(app, tmp_path, monkeypatch):
    snapshot = tmp_path / "2026-11-13.json"
    snapshot.write_text(json.dumps([product(i) for i in range(1200)]))
    price_runs.write_scrape_meta(snapshot, SCRAPED_AT)
    live = publish_run([product(i) for i in range(1200)], "newworld", SCRAPED_AT)
    again = price_runs.post_snapshot_file(snapshot, "newworld", dry_run=False)
    assert again.run_id == live.run_id and again.posted
    assert len(app.parts) == 2


# --- scrapedAt from a snapshot ---------------------------------------------------------

def test_snapshot_note_gives_the_scrape_time(tmp_path):
    snapshot = tmp_path / "2026-11-08.json"
    snapshot.write_text("[]")
    price_runs.write_scrape_meta(snapshot, "2026-11-08T08:01:02+13:00")
    assert price_runs.scraped_at_for_snapshot(snapshot) == "2026-11-08T08:01:02+13:00"


def test_snapshot_without_a_note_uses_the_files_own_time_in_nz(tmp_path):
    snapshot = tmp_path / "2026-11-13.json"
    snapshot.write_text("[]")
    stamp = datetime(2026, 11, 13, 9, 30, tzinfo=price_runs.NZ_TZ).timestamp()
    os.utime(snapshot, (stamp, stamp))
    assert price_runs.scraped_at_for_snapshot(snapshot) == "2026-11-13T09:30:00+13:00"


def test_snapshot_whose_file_time_is_another_day_falls_back_to_8am_that_day(tmp_path):
    snapshot = tmp_path / "2026-06-02.json"  # NZ winter: +12:00
    snapshot.write_text("[]")
    assert price_runs.scraped_at_for_snapshot(snapshot) == "2026-06-02T08:00:00+12:00"


def test_now_nz_iso_has_an_nz_offset():
    stamp = datetime.fromisoformat(price_runs.now_nz_iso())
    assert stamp.utcoffset().total_seconds() in (12 * 3600, 13 * 3600)


# --- the post_run command --------------------------------------------------------------

def test_post_run_dry_run_reports_the_plan(tmp_path, capsys):
    directory = tmp_path / "snaps"
    directory.mkdir()
    (directory / "2026-11-06.json").write_text(json.dumps([product(i) for i in range(5)]))
    (directory / "2026-11-13.json").write_text(json.dumps([product(i) for i in range(1500)]))
    (directory / "2026-11-13.meta.json").write_text(json.dumps({"scrapedAt": SCRAPED_AT}))
    code = post_run.main(["--site", "newworld", "--snapshots-dir", str(directory), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "1500 item(s) in 2 part(s)" in out and "scraped 2026-11-13T08:03:41+13:00" in out


def test_post_run_picks_a_given_date_and_posts(app, tmp_path, capsys):
    directory = tmp_path / "snaps"
    directory.mkdir()
    (directory / "2026-11-06.json").write_text(json.dumps([product(i) for i in range(5)]))
    (directory / "2026-11-13.json").write_text(json.dumps([product(i) for i in range(9)]))
    code = post_run.main(["--site", "newworld", "--snapshots-dir", str(directory), "--date", "2026-11-06"])
    assert code == 0
    body = json.loads(next(r for r in app.requests if r.get_method() == "POST").data)
    assert len(body["items"]) == 5 and body["runId"].startswith("newworld-2026-11-06-")
    assert "SUMMARY: run posted (parts 1/1, index complete)" in capsys.readouterr().out


def test_post_run_exits_non_zero_when_parts_are_missing(app, tmp_path):
    directory = tmp_path / "snaps"
    directory.mkdir()
    (directory / "2026-11-06.json").write_text(json.dumps([product(i) for i in range(5)]))
    app.fail[1] = ["500", "500", "500"]
    assert post_run.main(["--site", "newworld", "--snapshots-dir", str(directory)]) == 1


def test_latest_snapshot_ignores_note_files(tmp_path):
    (tmp_path / "2026-11-06.json").write_text("[]")
    (tmp_path / "2026-11-13.json").write_text("[]")
    (tmp_path / "2026-11-13.meta.json").write_text("{}")
    assert price_runs.latest_snapshot("newworld", tmp_path).name == "2026-11-13.json"
