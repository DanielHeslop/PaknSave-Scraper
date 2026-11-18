"""Ask the recipe app which search terms the scraped products satisfy.

POST <site>/api/match-terms runs the app's own matcher, so the app and the Pi
agree on what counts as found. The local matcher (scraper/matching.py) is only
the backup: any failure here raises AppMatchError and the caller falls back.

Transient failures (no connection, timeout, 5xx, 429) are retried a couple of
times with a pause, honouring Retry-After. Answers are cached for the run, so
a term already answered for the same products is not asked again; a change in
the app's rules version drops the cache and everything is asked again.

<site> is the origin of MAILBOX_URL; the Bearer token is MAILBOX_TOKEN. The token
is only ever put in the Authorization header, never logged or put in an error.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from scraper.auth import bearer

MAILBOX_URL_ENV = "MAILBOX_URL"
MAILBOX_TOKEN_ENV = "MAILBOX_TOKEN"
MATCH_PATH = "/api/match-terms"
TIMEOUT_SECONDS = 30
MAX_TERMS_PER_CALL = 300
MAX_PRODUCTS_PER_CALL = 6000
_READ_BLOCK = 65536
RETRY_DELAYS = (2.0, 6.0)  # pauses between the (len + 1) attempts at one call
MAX_RETRY_AFTER = 30.0


class AppMatchError(Exception):
    """The app matcher could not be used; the message is one short, token-free reason.

    retryable marks a failure worth another try (network, timeout, 5xx, 429);
    retry_after is the server's Retry-After in seconds, if it sent one.
    """

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class TermMatch:
    found: bool
    product_ids: tuple[str, ...] = ()
    via: str | None = None


@dataclass(frozen=True)
class AppMatches:
    version: str
    results: dict[str, TermMatch]


def match_url(mailbox_url: str) -> str:
    parts = urllib.parse.urlsplit(mailbox_url)
    if not parts.scheme or not parts.netloc:
        raise AppMatchError(f"{MAILBOX_URL_ENV} is not a full URL")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, MATCH_PATH, "", ""))


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    try:
        value = float(error.headers.get("Retry-After")) if error.headers else None
    except (TypeError, ValueError):
        return None
    return min(max(value, 0.0), MAX_RETRY_AFTER) if value is not None else None


def _post(url: str, token: str, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": bearer(token),
        },
    )
    deadline = time.monotonic() + timeout
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            chunks = []
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError
                block = response.read(_READ_BLOCK)
                if not block:
                    break
                chunks.append(block)
    except urllib.error.HTTPError as e:
        raise AppMatchError(
            f"status {e.code} ({e.reason})",
            retryable=e.code == 429 or e.code >= 500,
            retry_after=_retry_after(e),
        ) from None
    except TimeoutError:
        raise AppMatchError(f"timed out after {timeout:g}s", retryable=True) from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise AppMatchError(f"timed out after {timeout:g}s", retryable=True) from None
        raise AppMatchError(f"could not connect ({e.reason})", retryable=True) from None
    except OSError as e:
        raise AppMatchError(f"network error ({type(e).__name__})", retryable=True) from None
    if status != 200:
        raise AppMatchError(f"status {status}", retryable=status >= 500)
    try:
        data = json.loads(b"".join(chunks))
    except ValueError:
        raise AppMatchError("reply was not valid JSON") from None
    if not isinstance(data, dict):
        raise AppMatchError("reply was not a JSON object")
    return data


def _post_with_retries(url: str, token: str, body: dict, timeout: float) -> dict:
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            return _post(url, token, body, timeout)
        except AppMatchError as e:
            if not e.retryable or attempt == attempts - 1:
                if attempt:
                    raise AppMatchError(f"{e} after {attempt + 1} tries") from None
                raise
            time.sleep(e.retry_after if e.retry_after is not None else RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")


def _parse_reply(data: dict, sent_terms: list[str]) -> tuple[str, dict[str, TermMatch]]:
    version = data.get("version")
    results = data.get("results")
    if not isinstance(version, str) or not version or not isinstance(results, list):
        raise AppMatchError("reply is missing version or results")
    parsed: dict[str, TermMatch] = {}
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("term"), str) or not isinstance(item.get("found"), bool):
            raise AppMatchError("reply has a malformed result")
        ids = item.get("productIds") or []
        via = item.get("via")
        if not isinstance(ids, list) or not (via is None or isinstance(via, str)):
            raise AppMatchError("reply has a malformed result")
        parsed[item["term"]] = TermMatch(item["found"], tuple(str(i) for i in ids), via or None)
    missing = [t for t in sent_terms if t not in parsed]
    if missing:
        raise AppMatchError(f"reply has no result for {len(missing)} of {len(sent_terms)} term(s)")
    return version, parsed


def _merge(parts: list[TermMatch]) -> TermMatch:
    """Combine one term's answers from several product chunks.

    A chunk that found the term directly beats one that only found it via a
    fallback key, so fallback-only products are kept only if no chunk matched
    directly.
    """
    found = [p for p in parts if p.found]
    if not found:
        return TermMatch(False)
    direct = [p for p in found if p.via is None]
    chosen = direct or found
    ids = list(dict.fromkeys(i for p in chosen for i in p.product_ids))
    return TermMatch(True, tuple(ids), None if direct else chosen[0].via)


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


# (call URL, store, fingerprint of the products) -> (rules version, term -> answer)
_cache: dict[tuple[str, str, str], tuple[str, dict[str, TermMatch]]] = {}


def clear_cache() -> None:
    _cache.clear()


def _fingerprint(sent_products: list[dict]) -> str:
    digest = hashlib.sha256()
    for item in sorted(sent_products, key=lambda x: x["id"]):
        digest.update(f"{item['id']}\x00{item['name']}\x01".encode("utf-8"))
    return digest.hexdigest()


def _ask(url: str, token: str, store: str, terms: list[str], sent_products: list[dict], timeout: float,
         max_terms: int, max_products: int) -> tuple[str, dict[str, TermMatch]]:
    versions: list[str] = []
    answers: dict[str, list[TermMatch]] = {t: [] for t in terms}
    for term_chunk in _chunks(terms, max_terms):
        for product_chunk in _chunks(sent_products, max_products):
            body = {"store": store, "terms": term_chunk, "products": product_chunk}
            version, parsed = _parse_reply(_post_with_retries(url, token, body, timeout), term_chunk)
            if version not in versions:
                versions.append(version)
            for term in term_chunk:
                answers[term].append(parsed[term])
    return "/".join(versions), {t: _merge(parts) for t, parts in answers.items()}


def match_terms(
    store: str,
    terms: list[str],
    products: list[dict],
    *,
    timeout: float = TIMEOUT_SECONDS,
    max_terms: int = MAX_TERMS_PER_CALL,
    max_products: int = MAX_PRODUCTS_PER_CALL,
) -> AppMatches:
    """Match terms against products with the app's matcher. Raises AppMatchError.

    products are dicts with product_id and name. Terms and products beyond the
    per-call limits are split across calls and the answers merged; any failed
    call fails the whole thing, so a run never mixes two matchers. Terms already
    answered this run for the same products come from the cache; if the app's
    rules version has moved since they were cached, everything is asked again.
    """
    mailbox_url = os.environ.get(MAILBOX_URL_ENV)
    token = os.environ.get(MAILBOX_TOKEN_ENV)
    if not mailbox_url or not token:
        raise AppMatchError(f"{MAILBOX_URL_ENV}/{MAILBOX_TOKEN_ENV} not set")
    url = match_url(mailbox_url)

    terms = list(dict.fromkeys(terms))
    unique: dict[str, str] = {}
    for p in products:
        unique.setdefault(str(p["product_id"]), p["name"])
    sent_products = [{"id": i, "name": n} for i, n in unique.items()]
    key = (url, store, _fingerprint(sent_products))
    cached_version, cached = _cache.get(key, ("", {}))
    missing = [t for t in terms if t not in cached]
    if not missing:
        return AppMatches(cached_version, {t: cached[t] for t in terms})

    version, fresh = _ask(url, token, store, missing, sent_products, timeout, max_terms, max_products)
    if cached and version != cached_version:
        version, fresh = _ask(url, token, store, terms, sent_products, timeout, max_terms, max_products)
        cached = {}
    cached = {**cached, **fresh}
    _cache[key] = (version, cached)
    return AppMatches(version, {t: cached[t] for t in terms})
