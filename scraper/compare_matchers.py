"""Compare the app's matcher with the local one on a saved snapshot. Read-only:
nothing is published, reported or written.

    python -m scraper.compare_matchers --site newworld --snapshot data/newworld_snapshots/2026-11-06.json
    python -m scraper.compare_matchers --site woolworths          # the store's latest snapshot

Needs MAILBOX_URL / MAILBOX_TOKEN in the environment (source the env file first).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from scraper.app_matcher import AppMatchError, match_terms
from scraper.ingredients import DEFAULT_INGREDIENT_LIST_URL, STORES, fetch_store_lists
from scraper.matching import match_products
from scraper.search_step import dedupe_products, plan_terms


SNAPSHOT_DIRS = {"paknsave": "snapshots", "newworld": "newworld_snapshots", "woolworths": "woolworths_snapshots"}
DEFAULT_EXAMPLES = 15
_SNAPSHOT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def latest_snapshot(site: str, data_dir: Path | None = None) -> Path | None:
    directory = (data_dir or Path(__file__).resolve().parent.parent / "data") / SNAPSHOT_DIRS[site]
    names = sorted(p for p in directory.glob("*.json") if _SNAPSHOT_NAME.match(p.name)) if directory.is_dir() else []
    return names[-1] if names else None


def pair_differences(terms, app_ids: dict[str, set[str]], local_ids: dict[str, set[str]], by_id, via: dict[str, str]):
    """Every (term, product) the two matchers disagree on, as dicts, in term order."""
    out = []
    for term in terms:
        for pid in sorted(app_ids[term] ^ local_ids[term]):
            if pid not in by_id:
                continue
            in_app = pid in app_ids[term]
            out.append({
                "product": by_id[pid]["name"], "product_id": pid, "term": term,
                "local": "match" if pid in local_ids[term] else "no match",
                "app": ("match" + (f" (via {via[term]})" if term in via else "")) if in_app else "no match",
            })
    return out


def spread(items: list[dict], count: int) -> list[dict]:
    """`count` items taking turns across terms, so one chatty term does not fill the list."""
    queues: dict[str, list[dict]] = {}
    for item in items:
        queues.setdefault(item["term"], []).append(item)
    picked: list[dict] = []
    while len(picked) < count and any(queues.values()):
        for queue in queues.values():
            if queue and len(picked) < count:
                picked.append(queue.pop(0))
    return picked


def _names(ids, by_id) -> str:
    return "; ".join(f"{i} {by_id[i]['name']}" for i in ids if i in by_id) or "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", choices=STORES, required=True)
    parser.add_argument("--snapshot", help="a saved snapshot JSON file (default: the store's latest)")
    parser.add_argument("--examples", type=int, default=DEFAULT_EXAMPLES, help="how many differing pairs to list")
    parser.add_argument("--ingredient-list-url", default=DEFAULT_INGREDIENT_LIST_URL)
    args = parser.parse_args(argv)

    snapshot = Path(args.snapshot) if args.snapshot else latest_snapshot(args.site)
    if snapshot is None:
        print(f"No snapshot found for {args.site}.")
        return 1
    products = dedupe_products(json.load(open(snapshot)))
    lists = fetch_store_lists(args.site, args.ingredient_list_url)
    if lists is None:
        return 1
    terms, skipped = plan_terms(lists)
    by_id = {str(p["product_id"]): p for p in products}
    print(f"{args.site}: {len(terms)} term(s) ({len(skipped)} skipped), {len(products)} product(s) from {snapshot}")

    started = time.monotonic()
    try:
        app = match_terms(args.site, terms, products)
    except AppMatchError as e:
        print(f"App matcher call failed: {e}")
        return 1
    seconds = time.monotonic() - started

    local = {t: [str(p["product_id"]) for p in match_products(t, products)] for t in terms}
    app_found = {t for t in terms if app.results[t].found}
    local_found = {t for t in terms if local[t]}
    print(f"endpoint version {app.version}, call time {seconds:.2f}s")
    print(f"app   : {len(app_found)} found, {len(terms) - len(app_found)} not found")
    print(f"local : {len(local_found)} found, {len(terms) - len(local_found)} not found")
    via = {t: app.results[t].via for t in terms if app.results[t].via}
    print(f"found via a fallback key (app): {len(via)}")

    app_ids = {t: set(app.results[t].product_ids) for t in terms}
    local_ids = {t: set(local[t]) for t in terms}
    differences = pair_differences(terms, app_ids, local_ids, by_id, via)
    pairs = sum(len(local_ids[t]) for t in terms), sum(len(app_ids[t]) for t in terms)
    print(f"matched (term, product) pairs: local {pairs[0]}, app {pairs[1]}")
    print(f"pairs that differ: {len(differences)} ({sum(d['local'] == 'no match' for d in differences)} only the app matches, "
          f"{sum(d['app'] == 'no match' for d in differences)} only the local rules match)")
    print(f"\n{min(args.examples, len(differences))} example(s) (product | term | local | app):")
    for d in spread(differences, args.examples):
        print(f"- {d['product']} | {d['term']} | local {d['local']} | app {d['app']}")

    print("\nTerms where found / not found differs:")
    for t in terms:
        r = app.results[t]
        if r.found != bool(local[t]):
            via_note = f" via {r.via}" if r.via else ""
            print(f"- {t}: app {'found' + via_note if r.found else 'not found'}: {_names(r.product_ids, by_id)}")
            print(f"  {' ' * len(t)}  local {'found' if local[t] else 'not found'}: {_names(local[t], by_id)}")
    print("\nTerms found by both but with different products:")
    for t in terms:
        r = app.results[t]
        if r.found and local[t] and set(r.product_ids) != set(local[t]):
            print(f"- {t}: {len(r.product_ids)} by the app, {len(local[t])} locally")
            print(f"  app only  : {_names([i for i in r.product_ids if i not in local[t]], by_id)}")
            print(f"  local only: {_names([i for i in local[t] if i not in r.product_ids], by_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
