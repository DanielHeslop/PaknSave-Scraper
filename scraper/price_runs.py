"""Posting one scrape run to the recipe app's /api/price-runs endpoint.

A run is one store's whole shelf scrape. It is split into parts (at most
MAX_ITEMS_PER_PART items and MAX_PART_BYTES bytes each) and the parts are posted
one at a time, in order - the app cannot safely update its run list if two parts
arrive at once. Each part body is

  {"runId", "store", "scrapedAt", "part", "parts", "items"}

runId is store + scrape date + a short hash of the items, so posting the same run
again (same part numbers) replaces what the app already holds. scrapedAt is when
the shelf was actually scraped (NZ time, with offset), never when the post is sent.

After the last part the app's run list (GET ?index) is checked; if the run is not
complete, one rebuild (GET ?index&rebuild=1) is asked for and the list is checked
again. Parts that still fail are written to data/pending_runs/<store>.json and are
retried at the start of the next run (or by `python -m scraper.post_run`).

Same configuration as scraper/publish.py: MAILBOX_URL (its origin gives the site)
and MAILBOX_TOKEN (the Bearer token). PRICE_RUNS_URL overrides the endpoint.
Nothing here ever raises past publish_run, and the token is never printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scraper.publish import (
    MAILBOX_TOKEN_ENV,
    MAILBOX_URL_ENV,
    _fetch_excluded_keys,
    build_payload_items,
    publish_to_mailbox,
)

PRICE_RUNS_URL_ENV = "PRICE_RUNS_URL"
PRICE_RUNS_PATH = "/api/price-runs"

MAX_ITEMS_PER_PART = 1000
MAX_PART_BYTES = 2_000_000
PART_TIMEOUT_SECONDS = 90
INDEX_TIMEOUT_SECONDS = 30
ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (2, 6)
_ENVELOPE_MARGIN_BYTES = 64

NZ_TZ = ZoneInfo("Pacific/Auckland")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PENDING_DIR = os.path.join(REPO_ROOT, "data", "pending_runs")

# Where each store's snapshots live (the same directories the scrapers write to).
SNAPSHOT_SUBDIRS = {"paknsave": "snapshots", "newworld": "newworld_snapshots", "woolworths": "woolworths_snapshots"}
_SNAPSHOT_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def log(message: str) -> None:
    print(message, flush=True)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# --- times ---------------------------------------------------------------

def now_nz_iso() -> str:
    """The current time in NZ, with its UTC offset (e.g. 2026-11-14T08:00:12+13:00)."""
    return datetime.now(NZ_TZ).isoformat(timespec="seconds")


def meta_path(snapshot_path: str | Path) -> Path:
    return Path(snapshot_path).with_suffix(".meta.json")


def write_scrape_meta(snapshot_path: str | Path, scraped_at: str) -> None:
    """Remember when the shelf was scraped, next to the snapshot (best-effort)."""
    try:
        meta_path(snapshot_path).write_text(json.dumps({"scrapedAt": scraped_at}))
    except OSError as e:
        log(f"Could not write the scrape-time note next to the snapshot ({type(e).__name__}).")


def scraped_at_for_snapshot(snapshot_path: str | Path) -> str:
    """When this snapshot's shelf was scraped: its note, else the file's own time, else 8am that day."""
    snapshot_path = Path(snapshot_path)
    try:
        value = json.loads(meta_path(snapshot_path).read_text()).get("scrapedAt")
        if isinstance(value, str) and value:
            return value
    except (OSError, ValueError, AttributeError):
        pass
    file_date = snapshot_path.stem
    try:
        modified = datetime.fromtimestamp(snapshot_path.stat().st_mtime, NZ_TZ)
        if modified.date().isoformat() == file_date:
            return modified.isoformat(timespec="seconds")
    except OSError:
        pass
    return datetime.fromisoformat(f"{file_date}T08:00:00").replace(tzinfo=NZ_TZ).isoformat()


# --- run id and parts -----------------------------------------------------

def _canonical(item: dict) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":"))


def sort_items(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda i: (str(i.get("supermarket", "")), str(i.get("product_id", ""))))


def make_run_id(store: str, scraped_at: str, items: list[dict]) -> str:
    """store-<scrape date>-<10 hex of the items' hash>: unchanged if the same run is posted again."""
    digest = hashlib.sha256("\n".join(_canonical(i) for i in sort_items(items)).encode("utf-8")).hexdigest()
    return f"{store}-{scraped_at[:10]}-{digest[:10]}"


def _body(run_id: str, store: str, scraped_at: str, part: int, parts: int, items: list[dict]) -> bytes:
    body = {"runId": run_id, "store": store, "scrapedAt": scraped_at, "part": part, "parts": parts, "items": items}
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def split_items(
    items: list[dict],
    store: str = "paknsave",
    scraped_at: str = "2000-01-01T00:00:00+13:00",
    max_items: int = MAX_ITEMS_PER_PART,
    max_bytes: int = MAX_PART_BYTES,
) -> list[list[dict]]:
    """Split items (in order) into parts of at most max_items items and max_bytes bytes of request body.

    A single item that alone is bigger than max_bytes still gets a part of its own.
    """
    overhead = len(_body("x" * 60, store, scraped_at, 999, 999, [])) + _ENVELOPE_MARGIN_BYTES
    parts: list[list[dict]] = []
    current: list[dict] = []
    size = overhead
    for item in items:
        item_bytes = len(json.dumps(item, separators=(",", ":")).encode("utf-8")) + 1
        if current and (len(current) >= max_items or size + item_bytes > max_bytes):
            parts.append(current)
            current, size = [], overhead
        current.append(item)
        size += item_bytes
    if current:
        parts.append(current)
    return parts


@dataclass
class RunPlan:
    run_id: str
    store: str
    scraped_at: str
    items: list[dict]
    parts: list[list[dict]]

    def body(self, part: int) -> bytes:
        return _body(self.run_id, self.store, self.scraped_at, part, len(self.parts), self.parts[part - 1])


def plan_run(store: str, scraped_at: str, items: list[dict]) -> RunPlan:
    items = sort_items(items)
    run_id = make_run_id(store, scraped_at, items)
    return RunPlan(run_id, store, scraped_at, items, split_items(items, store, scraped_at))


# --- urls and requests -------------------------------------------------------

def price_runs_url(mailbox_url: str) -> str:
    override = os.environ.get(PRICE_RUNS_URL_ENV)
    if override:
        return override
    parts = urllib.parse.urlsplit(mailbox_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"{MAILBOX_URL_ENV} is not a full URL")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, PRICE_RUNS_PATH, "", ""))


class _RequestFailed(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def _request(url: str, token: str, method: str, body: bytes | None, timeout: float) -> bytes:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            data = response.read()
    except urllib.error.HTTPError as e:
        retryable = e.code in (408, 425, 429) or e.code >= 500
        raise _RequestFailed(f"status {e.code} ({e.reason})", retryable) from None
    except TimeoutError:
        raise _RequestFailed(f"timed out after {timeout:g}s", True) from None
    except urllib.error.URLError as e:
        raise _RequestFailed(f"could not connect ({e.reason})", True) from None
    except OSError as e:
        raise _RequestFailed(f"network error ({type(e).__name__})", True) from None
    if not 200 <= status < 300:
        raise _RequestFailed(f"status {status}", status >= 500)
    return data


def _with_retries(label: str, send) -> bytes | None:
    """Run send() up to ATTEMPTS times; None if every attempt failed (the reason is logged)."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return send()
        except _RequestFailed as e:
            final = attempt == ATTEMPTS or not e.retryable
            log(f"  {label}: attempt {attempt}/{ATTEMPTS} failed - {e}{'' if not final else ' - giving up'}.")
            if final:
                return None
            _sleep(RETRY_DELAYS_SECONDS[min(attempt - 1, len(RETRY_DELAYS_SECONDS) - 1)])
    return None


def post_part(url: str, token: str, body: bytes, label: str) -> bool:
    return _with_retries(label, lambda: _request(url, token, "POST", body, PART_TIMEOUT_SECONDS)) is not None


# --- the app's run list --------------------------------------------------

def _find_run(data, run_id: str):
    if isinstance(data, dict):
        if data.get("runId") == run_id or data.get("id") == run_id:
            return data
        if isinstance(data.get(run_id), dict):
            return data[run_id]
        children = data.values()
    elif isinstance(data, list):
        children = data
    else:
        return None
    for child in children:
        found = _find_run(child, run_id)
        if found is not None:
            return found
    return None


def _is_complete(run: dict) -> bool:
    if run.get("complete") is True:
        return True
    return str(run.get("status", "")).lower() == "complete"


def check_index(url: str, token: str, run_id: str, rebuild: bool = False) -> str:
    """"complete", "incomplete" (listed but not complete, or not listed) or "unknown" (could not be read)."""
    suffix = "?index&rebuild=1" if rebuild else "?index"
    raw = _with_retries(
        "run list" + (" (rebuild)" if rebuild else ""),
        lambda: _request(url + suffix, token, "GET", None, INDEX_TIMEOUT_SECONDS),
    )
    if raw is None:
        return "unknown"
    try:
        data = json.loads(raw)
    except ValueError:
        log("  run list: the reply was not valid JSON.")
        return "unknown"
    run = _find_run(data, run_id)
    if run is None:
        return "incomplete"
    return "complete" if _is_complete(run) else "incomplete"


def verify_in_index(url: str, token: str, run_id: str) -> tuple[str, bool]:
    """Check the run is complete; if not, rebuild the list once and check again. Returns (state, rebuilt)."""
    state = check_index(url, token, run_id)
    if state == "complete":
        return state, False
    log("  The run list does not show the run as complete - asking the app to rebuild it once.")
    check_index(url, token, run_id, rebuild=True)
    return check_index(url, token, run_id), True


# --- pending (failed) parts ----------------------------------------------------

def _pending_file(store: str) -> Path:
    return Path(PENDING_DIR) / f"{store}.json"


def _load_pending(store: str) -> dict:
    try:
        data = json.loads(_pending_file(store).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pending(store: str, pending: dict) -> None:
    path = _pending_file(store)
    try:
        if not pending:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pending, indent=1, sort_keys=True))
    except OSError as e:
        log(f"Could not update the list of runs with missing parts ({type(e).__name__}).")


def _record_pending(store: str, run_id: str, scraped_at: str, parts: int, missing: list[int], snapshot_path) -> None:
    pending = _load_pending(store)
    if missing and snapshot_path:
        pending[run_id] = {
            "runId": run_id,
            "scrapedAt": scraped_at,
            "parts": parts,
            "missing": missing,
            "snapshot": str(snapshot_path),
        }
    else:
        pending.pop(run_id, None)
    _save_pending(store, pending)


# --- result --------------------------------------------------------------

@dataclass
class RunResult:
    items: int = 0  # items in the parts the app accepted
    run_id: str | None = None
    parts: int = 0
    missing: list[int] = field(default_factory=list)
    index: str | None = None  # "complete", "incomplete", "unknown" or None (not checked)
    rebuilt: bool = False
    note: str | None = None  # why nothing was posted

    @property
    def posted(self) -> bool:
        return self.run_id is not None and self.parts > 0 and not self.missing

    def summary(self) -> str | None:
        """The text added to the SUMMARY line, or None when no post was tried."""
        if self.note:
            return f"run not posted ({self.note})"
        if self.run_id is None:
            return None
        if self.missing:
            return f"run failed (missing parts {', '.join(str(p) for p in self.missing)})"
        index = {"complete": "index complete", "incomplete": "index NOT complete", "unknown": "index could not be checked"}
        return f"run posted (parts {self.parts}/{self.parts}, {index.get(self.index or '', 'index not checked')})"


# --- posting ---------------------------------------------------------------

def _items_for(products: list[dict], mailbox_url: str | None, token: str | None) -> list[dict]:
    items = build_payload_items(products)
    if items and mailbox_url and token:
        excluded = _fetch_excluded_keys(mailbox_url, token)
        if excluded is not None:
            kept = [i for i in items if f"{i['supermarket']}|{i['product_id']}" not in excluded]
            log(f"Excluded {len(items) - len(kept)} of {len(items)} items per scrape-exclusions list")
            items = kept
    return items


def describe_plan(plan: RunPlan) -> None:
    log(f"Run {plan.run_id}: {len(plan.items)} item(s) in {len(plan.parts)} part(s), scrapedAt {plan.scraped_at}.")
    for number, part in enumerate(plan.parts, start=1):
        log(f"  part {number}/{len(plan.parts)}: {len(part)} item(s), {len(plan.body(number)):,} bytes")


def publish_run(
    products: list[dict],
    store: str,
    scraped_at: str,
    *,
    dry_run: bool = False,
    legacy: bool = False,
    snapshot_path: str | Path | None = None,
    only_parts: set[int] | None = None,
    expect_run_id: str | None = None,
    retry_pending_first: bool = True,
) -> RunResult:
    """Post one run (best-effort, never raises). See the module docstring.

    legacy=True posts to the old mailbox instead (and nothing else). only_parts and
    expect_run_id are for retrying parts that failed earlier: if the run's id has
    changed since, the whole run is posted under its new id.
    """
    try:
        return _publish_run(
            products, store, scraped_at, dry_run, legacy, snapshot_path, only_parts, expect_run_id, retry_pending_first
        )
    except Exception as e:
        log(f"Posting the run failed unexpectedly ({type(e).__name__}).")
        return RunResult(note=f"unexpected {type(e).__name__}")


def _publish_run(products, store, scraped_at, dry_run, legacy, snapshot_path, only_parts, expect_run_id, retry_first):
    if legacy:
        sent = publish_to_mailbox(products, dry_run=dry_run) or 0
        return RunResult(items=sent, note="legacy mailbox")

    mailbox_url = os.environ.get(MAILBOX_URL_ENV)
    token = os.environ.get(MAILBOX_TOKEN_ENV)
    configured = bool(mailbox_url and token)
    if not configured and not dry_run:
        log("Publishing to the recipe app skipped - MAILBOX_URL/MAILBOX_TOKEN not set.")
        return RunResult(note=f"{MAILBOX_URL_ENV}/{MAILBOX_TOKEN_ENV} not set")

    items = _items_for(products, mailbox_url, token)
    if not items:
        log("Publishing to the recipe app skipped - no priced products this run.")
        return RunResult(note="no priced products")

    plan = plan_run(store, scraped_at, items)
    describe_plan(plan)
    if dry_run:
        log("Dry run - nothing posted.")
        return RunResult(run_id=plan.run_id, parts=len(plan.parts), note="dry run")

    if retry_first and only_parts is None:
        retry_pending(store)

    url = price_runs_url(mailbox_url)
    wanted = range(1, len(plan.parts) + 1)
    if only_parts is not None and expect_run_id == plan.run_id:
        wanted = sorted(only_parts)
    elif only_parts is not None:
        log(f"The run's id is now {plan.run_id} (was {expect_run_id}) - posting all of its parts.")

    sent_items, missing = 0, []
    for number in wanted:
        label = f"part {number}/{len(plan.parts)}"
        if post_part(url, token, plan.body(number), label):
            sent_items += len(plan.parts[number - 1])
            log(f"  {label} posted ({len(plan.parts[number - 1])} items).")
        else:
            missing.append(number)

    result = RunResult(items=sent_items, run_id=plan.run_id, parts=len(plan.parts), missing=missing)
    _record_pending(store, plan.run_id, scraped_at, len(plan.parts), missing, snapshot_path)
    if expect_run_id and expect_run_id != plan.run_id:
        _record_pending(store, expect_run_id, scraped_at, 0, [], None)
    if missing:
        log(f"Run {plan.run_id}: parts {missing} did not go through - they will be retried next run or by post_run.")
        return result

    result.index, result.rebuilt = verify_in_index(url, token, plan.run_id)
    log(f"Run {plan.run_id}: {result.summary()}" + (" (after a rebuild)" if result.rebuilt else ""))
    return result


def retry_pending(store: str) -> None:
    """Re-post parts that failed on an earlier run, from the snapshot saved then. Never raises."""
    try:
        pending = _load_pending(store)
        for run_id, record in sorted(pending.items()):
            snapshot = Path(record.get("snapshot", ""))
            try:
                products = json.loads(snapshot.read_text())
            except (OSError, ValueError):
                log(f"Earlier run {run_id} has missing parts but its snapshot can't be read - dropped.")
                _record_pending(store, run_id, "", 0, [], None)
                continue
            log(f"Retrying missing parts {record.get('missing')} of the earlier run {run_id}.")
            publish_run(
                products,
                store,
                record["scrapedAt"],
                snapshot_path=snapshot,
                only_parts=set(record.get("missing") or []),
                expect_run_id=run_id,
                retry_pending_first=False,
            )
    except Exception as e:
        log(f"Retrying earlier runs failed unexpectedly ({type(e).__name__}).")


# --- from a saved snapshot -----------------------------------------------------

def latest_snapshot(store: str, snapshots_dir: str | Path | None = None) -> Path | None:
    directory = Path(snapshots_dir or os.path.join(REPO_ROOT, "data", SNAPSHOT_SUBDIRS[store]))
    if not directory.is_dir():
        return None
    names = sorted(p for p in directory.iterdir() if _SNAPSHOT_NAME.match(p.name))
    return names[-1] if names else None


def post_snapshot_file(path: str | Path, store: str, *, dry_run: bool, legacy: bool = False) -> RunResult | None:
    """Post a saved snapshot as a run. None if the file can't be read."""
    path = Path(path)
    try:
        products = json.loads(path.read_text())
    except FileNotFoundError:
        log(f"Snapshot not found: {path}")
        return None
    except ValueError as e:
        log(f"Snapshot at {path} is not valid JSON: {e}")
        return None
    if not isinstance(products, list):
        log(f"Snapshot at {path} is not a JSON array of products.")
        return None
    scraped_at = scraped_at_for_snapshot(path)
    log(f"Loaded {len(products)} product(s) from {path} (scraped {scraped_at}).")
    return publish_run(products, store, scraped_at, dry_run=dry_run, legacy=legacy, snapshot_path=path.resolve())
