"""The "search step" shared by every chain: decide which of the app's terms were
found, which weren't, and tell the app.

Pak'nSave / New World: the site's search pages are disallowed by robots.txt, so
terms are matched locally against the products the category scrape just
collected (no network). Woolworths uses its API instead, but builds the same
SearchOutcome so reporting and the run summary are identical.

A term is only reported "not found" when it could have been found: a category
that holds that kind of product was scraped this run (Foodstuffs), or the
search itself ran and came back empty (Woolworths). Anything else is left
unreported rather than guessed at.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from scraper.app_matcher import AppMatchError, AppMatches, match_terms
from scraper.ingredients import StoreLists, report_found, report_not_found
from scraper.matching import _stem, match_products

SEARCH_TIME_BUDGET_SECONDS = 20 * 60

_WORD = re.compile(r"[a-z]+")


def log(message: str) -> None:
    print(message, flush=True)


def product_key(product: dict) -> str:
    return f"{product.get('supermarket', '')}|{product['product_id']}"


def dedupe_products(products: list[dict]) -> list[dict]:
    """One entry per (supermarket, product_id), first occurrence wins."""
    seen: set[str] = set()
    out: list[dict] = []
    for product in products:
        key = product_key(product)
        if key not in seen:
            seen.add(key)
            out.append(product)
    return out


@dataclass
class SearchOutcome:
    searched: list[str] = field(default_factory=list)
    found: dict[str, list[dict]] = field(default_factory=dict)
    not_found: list[str] = field(default_factory=list)
    unreported: list[str] = field(default_factory=list)  # searched, nothing matched, but no category to blame
    skipped: list[str] = field(default_factory=list)  # app says skip
    unsearched: list[str] = field(default_factory=list)  # ran out of time
    matcher: str = "none"  # "app <version>", "local fallback", or "none" if nothing was searched
    via: dict[str, str] = field(default_factory=dict)  # term -> fallback key that found it (app matcher only)

    @property
    def matched_products(self) -> list[dict]:
        return dedupe_products([p for matches in self.found.values() for p in matches])


def plan_terms(lists: StoreLists) -> tuple[list[str], list[str]]:
    """Return (terms to search, skipped terms).

    Order: the app's search list first (its own order, most useful first), then
    the watch terms. Skipped search terms are dropped; watch terms are always
    searched, since watching them is the point. Duplicates (any case) are
    collapsed.
    """
    to_search: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()

    def add(term: str, allow_skip: bool) -> None:
        key = " ".join(term.lower().split())
        if key in seen:
            return
        seen.add(key)
        if allow_skip and lists.is_skipped(term):
            skipped.append(term)
        else:
            to_search.append(term)

    watch_keys = {" ".join(t.lower().split()) for t in lists.watch_terms}
    for term in lists.search_terms:
        add(term, allow_skip=" ".join(term.lower().split()) not in watch_keys)
    for term in lists.watch_terms:
        add(term, allow_skip=False)
    return to_search, skipped


def category_words(label: str, covers: tuple[str, ...] | list[str]) -> set[str]:
    words = set(_WORD.findall(label.lower())) | set(covers)
    return {_stem(w) for w in words if len(w) >= 3}


def term_has_scraped_home(term: str, scraped_words: set[str]) -> bool:
    return bool({_stem(w) for w in _WORD.findall(term.lower()) if len(w) >= 3} & scraped_words)


def local_search(
    terms: list[str],
    products: list[dict],
    scraped_words: set[str],
    deadline: float | None = None,
) -> SearchOutcome:
    """Match terms against already-scraped products. No network.

    scraped_words: category_words() unioned over the categories that loaded
    this run. A term nothing matched is "not found" only if it shares a word
    with those; otherwise it lands in `unreported`.
    """
    outcome = SearchOutcome()
    for index, term in enumerate(terms):
        if deadline is not None and time.monotonic() > deadline:
            outcome.unsearched = terms[index:]
            log(f"Search step ran out of time - {len(outcome.unsearched)} term(s) not searched.")
            break
        outcome.searched.append(term)
        matches = match_products(term, products)
        if matches:
            outcome.found[term] = matches
        elif term_has_scraped_home(term, scraped_words):
            outcome.not_found.append(term)
        else:
            outcome.unreported.append(term)
    return outcome


def local_fallback(reason: str) -> None:
    log(f"App matcher unavailable ({reason}) - using the local matcher for this run.")


def try_app_match(store: str, terms: list[str], products: list[dict]) -> AppMatches | None:
    """The app's verdict on every term, or None (after one log line) if it can't be had."""
    if not terms:
        return None
    try:
        return match_terms(store, terms, products)
    except AppMatchError as e:
        local_fallback(str(e))
    except Exception as e:
        local_fallback(f"unexpected error, {type(e).__name__}")
    return None


def outcome_from_app(
    matches: AppMatches, terms: list[str], products: list[dict], reportable: Callable[[str], bool]
) -> SearchOutcome:
    """Turn the app's answers into a SearchOutcome.

    reportable(term) says whether a term nothing matched may be reported "not
    found" (otherwise it goes to `unreported`).
    """
    by_id = {str(p["product_id"]): p for p in products}
    outcome = SearchOutcome(searched=list(terms), matcher=f"app {matches.version}")
    for term in terms:
        result = matches.results[term]
        if result.found:
            outcome.found[term] = [by_id[i] for i in result.product_ids if i in by_id]
            if result.via:
                outcome.via[term] = result.via
        elif reportable(term):
            outcome.not_found.append(term)
        else:
            outcome.unreported.append(term)
    return outcome


def search_products_for_terms(
    store: str,
    terms: list[str],
    products: list[dict],
    scraped_words: set[str],
    deadline: float | None = None,
) -> SearchOutcome:
    """Foodstuffs search step: the app's matcher, or the local one if the app can't be used."""
    products = dedupe_products(products)
    matches = try_app_match(store, terms, products)
    if matches is not None:
        return outcome_from_app(matches, terms, products, lambda t: term_has_scraped_home(t, scraped_words))
    outcome = local_search(terms, products, scraped_words, deadline)
    if terms:
        outcome.matcher = "local fallback"
    return outcome


def report_outcome(store: str, outcome: SearchOutcome) -> None:
    """Send found / not-found to the app. Never raises (the report_* calls swallow failures)."""
    report_found(store, list(outcome.found))
    report_not_found(store, outcome.not_found)


def log_outcome(outcome: SearchOutcome) -> None:
    log(
        f"Search step: {len(outcome.searched)} term(s) searched, {len(outcome.found)} found, "
        f"{len(outcome.not_found)} not found, {len(outcome.unreported)} no match but not reported "
        f"(no category scraped for them), {len(outcome.skipped)} skipped, "
        f"{len(outcome.unsearched)} out of time."
    )
    for term, matches in outcome.found.items():
        log(f"  FOUND     {term!r}: {len(matches)} product(s)")
    for term, key in outcome.via.items():
        log(f"  {term}: found via {key}")
    for term in outcome.not_found:
        log(f"  NOT FOUND {term!r}")
    for term in outcome.unreported:
        log(f"  no match, not reported: {term!r}")


def summary_line(
    products_published: int,
    outcome: SearchOutcome,
    categories_scraped: int,
    seconds: float,
    mapping: dict | None = None,
    run_status: str | None = None,
) -> str:
    """The one-line run summary. `mapping` (mapped / new / not_found counts) adds the watch-category part;
    `run_status` ("run posted (parts 3/3, index complete)" or "run failed (missing parts 2)") is added at the end."""
    minutes, secs = divmod(int(seconds), 60)
    watch = ""
    if mapping is not None:
        watch = (
            f"watch categories mapped {mapping['mapped']}, newly mapped {mapping['new']}, "
            f"not found {mapping['not_found']}, "
        )
    return (
        f"SUMMARY: products published {products_published}, terms searched {len(outcome.searched)}, "
        f"found {len(outcome.found)}, not found {len(outcome.not_found)}, skipped {len(outcome.skipped)}, "
        f"categories scraped {categories_scraped}, {watch}matcher {outcome.matcher}, "
        f"found via fallback key {len(outcome.via)}, run time {minutes}m{secs:02d}s"
        + (f", {run_status}" if run_status else "")
    )
