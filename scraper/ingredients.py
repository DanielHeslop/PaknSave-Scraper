"""Fetch the "wanted" ingredient list from the recipe app.

The recipe app (the same one scraper/publish.py sends prices to) exposes a
read-only endpoint listing every ingredient name used across its recipes.
This module only ever GETs that list - it never writes anything back to the
recipe app itself (that's publish.py's job, and only for matched results).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULT_INGREDIENT_LIST_URL = "https://my-recipe-manager.netlify.app/api/ingredient-list"
TIMEOUT_SECONDS = 15

# Same env var scraper/publish.py reads its Bearer token from - the app's
# ingredient-list, price-mailbox, and scrape-exclusions endpoints all accept
# this one scraper token, so there's only ever one credential to configure.
MAILBOX_TOKEN_ENV = "MAILBOX_TOKEN"


class IngredientListError(Exception):
    """Raised when the ingredient list can't be fetched or parsed at all.

    Unlike publish.py's best-effort failures, this one is fatal to a search
    run - without a list of ingredients there is nothing to search for.
    """


def fetch_ingredient_list(url: str = DEFAULT_INGREDIENT_LIST_URL) -> list[str]:
    """GET the ingredient list and return it as a plain list of names.

    Sends "Authorization: Bearer <MAILBOX_TOKEN>" when that env var is set -
    same env var and header scraper/publish.py uses for price-mailbox and
    scrape-exclusions, since the app expects the same scraper token on all
    three endpoints. If MAILBOX_TOKEN is unset, the request is sent without
    an Authorization header, same as before (the app may still allow this
    endpoint to be read anonymously; if not, the resulting 401 surfaces via
    IngredientListError same as any other failure).

    Raises IngredientListError with a clear reason on any failure (network,
    HTTP status, or unexpected JSON shape) rather than returning a partial
    or guessed-at list. Never logs the token itself.
    """
    headers = {"Accept": "application/json"}
    mailbox_token = os.environ.get(MAILBOX_TOKEN_ENV)
    if mailbox_token:
        headers["Authorization"] = f"Bearer {mailbox_token}"
    request = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as e:
        raise IngredientListError(f"{url} responded with status {e.code} ({e.reason})") from e
    except urllib.error.URLError as e:
        raise IngredientListError(f"could not reach {url} ({e.reason})") from e
    except TimeoutError as e:
        raise IngredientListError(f"{url} timed out after {TIMEOUT_SECONDS}s") from e

    if not (200 <= status < 300):
        raise IngredientListError(f"{url} responded with status {status}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise IngredientListError(f"{url} did not return valid JSON: {e}") from e

    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise IngredientListError(f"{url} did not return a JSON array of strings")

    return data


STORES = ("paknsave", "newworld", "woolworths")


def _log(message: str) -> None:
    print(message, flush=True)


def _normalise_term(term: str) -> str:
    return " ".join(term.lower().split())


@dataclass(frozen=True)
class WatchCategory:
    """One watch category from the app: a name, plus the store paths it already knows.

    paths maps a store ("paknsave", "newworld", "woolworths") to that store's
    category path; a store may be missing or blank. A plain-string watch category
    has no paths at all.
    """

    name: str
    paths: dict[str, str] = field(default_factory=dict)

    def path_for(self, store: str) -> str:
        return (self.paths.get(store) or "").strip()


@dataclass(frozen=True)
class StoreLists:
    """One store's lists from the app's ingredient-list endpoint (?store=<store>).

    search_terms keeps the app's order (most useful first). skip holds
    normalised (lower-case, single-spaced) terms the app doesn't want
    searched at this store right now - use is_skipped() rather than testing
    membership by hand.
    """

    store: str
    search_terms: list[str] = field(default_factory=list)
    watch_terms: list[str] = field(default_factory=list)
    watch_categories: list[str] = field(default_factory=list)
    watch_category_entries: list[WatchCategory] = field(default_factory=list)
    skip: frozenset[str] = frozenset()

    def is_skipped(self, term: str) -> bool:
        return _normalise_term(term) in self.skip

    def watch_category_specs(self) -> list[WatchCategory]:
        """The watch categories with whatever paths the app gave (names only if it gave none)."""
        return self.watch_category_entries or [WatchCategory(name) for name in self.watch_categories]


def _store_url(url: str, store: str, action: str | None = None) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k not in ("store", "action")]
    query.append(("store", store))
    if action:
        query.append(("action", action))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _auth_headers(extra: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if extra:
        headers.update(extra)
    token = os.environ.get(MAILBOX_TOKEN_ENV)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _string_list(value, what: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{what} is not a list")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        term = item.get("term") if isinstance(item, dict) else item
        if not isinstance(term, str):
            raise ValueError(f"{what} has an entry that is not a term")
        term = term.strip()
        key = _normalise_term(term)
        if term and key not in seen:
            seen.add(key)
            out.append(term)
    return out


def _watch_categories(value) -> list[WatchCategory]:
    """watch.categories: plain strings and/or {"name": ..., "paths": {store: path}} objects."""
    if not isinstance(value, list):
        raise ValueError("watch.categories is not a list")
    out: list[WatchCategory] = []
    seen: set[str] = set()
    for item in value:
        paths: dict[str, str] = {}
        if isinstance(item, dict):
            name = item.get("name")
            raw_paths = item.get("paths") or {}
            if not isinstance(raw_paths, dict):
                raise ValueError("watch.categories has an entry whose paths is not an object")
            paths = {str(k): v.strip() for k, v in raw_paths.items() if isinstance(v, str) and v.strip()}
        else:
            name = item
        if not isinstance(name, str):
            raise ValueError("watch.categories has an entry that is not a category")
        name = name.strip()
        key = _normalise_term(name)
        if name and key not in seen:
            seen.add(key)
            out.append(WatchCategory(name, paths))
    return out


def fetch_store_lists(
    store: str, url: str = DEFAULT_INGREDIENT_LIST_URL, timeout: int = TIMEOUT_SECONDS
) -> StoreLists | None:
    """GET <url>?store=<store> and return that store's lists, or None on any failure.

    FAILS OPEN: a network error, non-2xx status, or unexpected response shape
    logs one short line (never the token) and returns None, so the caller can
    carry on without the search step. Never raises.
    """
    full_url = _store_url(url, store)
    request = urllib.request.Request(full_url, headers=_auth_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
        if not (200 <= status < 300):
            raise ValueError(f"status {status}")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("response is not a JSON object")
        watch = data.get("watch")
        if watch is None:
            watch = {}
        if not isinstance(watch, dict):
            raise ValueError("watch is not an object")
        watch_entries = _watch_categories(watch.get("categories") or [])
        return StoreLists(
            store=store,
            search_terms=_string_list(data.get("search") or [], "search"),
            watch_terms=_string_list(watch.get("terms") or [], "watch.terms"),
            watch_categories=[entry.name for entry in watch_entries],
            watch_category_entries=watch_entries,
            skip=frozenset(_normalise_term(t) for t in _string_list(data.get("skip") or [], "skip")),
        )
    except urllib.error.HTTPError as e:
        reason = f"status {e.code} ({e.reason})"
    except urllib.error.URLError as e:
        reason = f"could not connect ({e.reason})"
    except TimeoutError:
        reason = f"timed out after {timeout}s"
    except (ValueError, UnicodeDecodeError) as e:
        reason = f"unexpected response ({e})"
    except Exception as e:
        reason = f"unexpected error ({type(e).__name__})"
    _log(f"Could not fetch the {store} search list: {reason} - continuing without the search step.")
    return None


def _post_terms(store: str, action: str, payload: dict, url: str, timeout: int = TIMEOUT_SECONDS) -> bool:
    request = urllib.request.Request(
        _store_url(url, store, action),
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=_auth_headers({"Content-Type": "application/json"}),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
        if 200 <= status < 300:
            return True
        reason = f"status {status}"
    except urllib.error.HTTPError as e:
        reason = f"status {e.code} ({e.reason})"
    except urllib.error.URLError as e:
        reason = f"could not connect ({e.reason})"
    except TimeoutError:
        reason = f"timed out after {timeout}s"
    except Exception as e:
        reason = f"unexpected error ({type(e).__name__})"
    _log(f"Reporting {action} terms for {store} failed: {reason} - ignoring.")
    return False


def report_not_found(
    store: str,
    terms: list[str],
    url: str = DEFAULT_INGREDIENT_LIST_URL,
    searched_at: str | None = None,
) -> bool:
    """Tell the app these terms had no results at this store. Never raises."""
    if not terms:
        return True
    searched_at = searched_at or datetime.now(timezone.utc).isoformat()
    return _post_terms(store, "not-found", {"terms": list(terms), "searchedAt": searched_at}, url)


def report_found(store: str, terms: list[str], url: str = DEFAULT_INGREDIENT_LIST_URL) -> bool:
    """Tell the app these terms found something at this store (resets not-found). Never raises."""
    if not terms:
        return True
    return _post_terms(store, "found", {"terms": list(terms)}, url)
