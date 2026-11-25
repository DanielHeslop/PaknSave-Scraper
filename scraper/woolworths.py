"""Entry point for the Woolworths NZ scrape, in three steps:

  0. Category tree: the store's categories (departments, aisles, shelves) are
     sent to the app when they changed, and the app's watch categories are
     mapped onto them by name (or the path the app gives).
  1. Browse: every path in woolworths_categories.txt, plus the mapped watch
     categories, is read through the public browse API.
  2. Search: the app's list for this store (GET ?store=woolworths) gives the terms;
     each is looked up through the public product-search API with a short delay
     between terms, time-boxed to about 20 minutes. The app's matcher
     (POST /api/match-terms; the local matcher is the backup) then decides found /
     not-found over everything browsed and searched, and that is reported back
     to the app. A failure here never stops step 3.
  3. Publish: browse + matched search results, de-duplicated by product id, go to
     the mailbox. Runs even if step 2 failed.

robots.txt is fetched once per run; it disallows /shop/search (the HTML page) but
not /api/v1/products, which is all this uses. Plain HTTP, no browser.

Default mode is a DRY RUN: nothing is written, published or reported. Pass --save
to do it for real.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from scraper import checkpoint, cooking_wine, lock, page_limits, power
from scraper.category_mapping import Mapping, MappingResult, covers_for, label_for, sync_and_map
from scraper.extract import is_valid_product
from scraper.ingredients import DEFAULT_INGREDIENT_LIST_URL, fetch_store_lists
from scraper.matching import match_products
from scraper.price_runs import now_nz_iso, publish_run, write_scrape_meta
from scraper.robots import RobotsDisallowed
from scraper.search_step import (
    SEARCH_TIME_BUDGET_SECONDS,
    SearchOutcome,
    dedupe_products,
    product_key,
    log_outcome,
    outcome_from_app,
    plan_terms,
    report_outcome,
    summary_line,
    try_app_match,
)
from scraper.storage import update_price_history, write_snapshot
from scraper import woolworths_shelves
from scraper.woolworths_sheets import enrich_sheet_counts
from scraper.woolworths_transport import (
    BotDetected,
    browse_products,
    get_product_description_text,
    search_products,
)

STORE = "woolworths"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TERMS_PATH = os.path.join(REPO_ROOT, "scraper", "woolworths_terms.txt")
DEFAULT_CATEGORIES_PATH = os.path.join(REPO_ROOT, "woolworths_categories.txt")
# Own snapshot directory: write_snapshot() replaces a whole <date>.json file, so
# sharing PAK'nSAVE's directory would clobber it. price_history.json is shared
# (keyed per product_id, append-only).
DEFAULT_SNAPSHOTS_DIR = os.path.join(REPO_ROOT, "data", "woolworths_snapshots")
DEFAULT_HISTORY_PATH = os.path.join(REPO_ROOT, "data", "price_history.json")
DEFAULT_BROWSE_MAX_PAGES = 5


def log(message: str) -> None:
    print(message, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Woolworths NZ price scraper (read-only, plain HTTP against the public search API)."
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Write a snapshot and update price_history.json. Default is a dry run (prints only).",
    )
    parser.add_argument(
        "--terms",
        default=DEFAULT_TERMS_PATH,
        help="Fallback term list, used only if the app's list can't be fetched.",
    )
    parser.add_argument("--categories", default=DEFAULT_CATEGORIES_PATH)
    parser.add_argument("--ingredient-list-url", default=DEFAULT_INGREDIENT_LIST_URL)
    parser.add_argument("--snapshots-dir", default=DEFAULT_SNAPSHOTS_DIR)
    parser.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)
    parser.add_argument(
        "--legacy-mailbox",
        action="store_true",
        help="Post to the old mailbox instead of /api/price-runs (fallback only).",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=30,
        help="Max results to request per search term (default 30).",
    )
    return parser.parse_args(argv)


def parse_terms_file(path: str | Path) -> list[str]:
    """Read woolworths_terms.txt: one search term per non-comment, non-blank line."""
    path = Path(path)
    if not path.exists():
        return []
    terms = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(line)
    return terms


@dataclass(frozen=True)
class BrowsePage:
    path: str
    label: str
    covers: tuple[str, ...] = ()
    max_pages: int = DEFAULT_BROWSE_MAX_PAGES


def parse_categories_file(path: str | Path) -> list[BrowsePage]:
    """Read woolworths_categories.txt: `<dept>/<aisle>[/<shelf>] [pages=N] [category=x] [covers=a,b]`."""
    path = Path(path)
    if not path.exists():
        return []
    pages: list[BrowsePage] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(woolworths_shelves.RULE_PREFIX):
            continue
        parts = line.split()
        browse_path = parts[0].strip("/")
        label = browse_path.split("/")[-1]
        covers: tuple[str, ...] = ()
        max_pages = DEFAULT_BROWSE_MAX_PAGES
        for part in parts[1:]:
            if part.startswith("category="):
                label = part.split("=", 1)[1]
            elif part.startswith("covers="):
                covers = tuple(w for w in part.split("=", 1)[1].lower().split(",") if w)
            elif part.startswith("pages="):
                try:
                    max_pages = max(1, int(part.split("=", 1)[1]))
                except ValueError:
                    pass
        pages.append(BrowsePage(browse_path, label, covers, max_pages))
    return pages


def extra_browse_pages(mapped: list[Mapping], existing: list[BrowsePage]) -> list[BrowsePage]:
    """Browse pages for mapped watch categories, leaving out any the categories file already reads."""
    have = {page.path for page in existing}
    pages = []
    for m in mapped:
        if m.path in have:
            continue
        have.add(m.path)
        pages.append(BrowsePage(m.path, label_for(m), covers_for(m), DEFAULT_BROWSE_MAX_PAGES))
    return pages


def planned_pages(
    categories_path: str | Path, tree_paths, pages: list[BrowsePage], log=log
) -> tuple[list[BrowsePage], list[str]]:
    """`pages` plus the aisles named by the `@aisles` rules that the tree has, and the log lines for it.

    A page that lies inside one of the aisles (or repeats a path) is folded into the aisle read whole,
    which keeps its `covers`.
    """
    tree_paths = list(tree_paths)
    rules = woolworths_shelves.parse_rules(categories_path)
    notes: list[str] = []
    if not rules:
        return pages, notes
    if not tree_paths:
        notes.append("Woolworths shelves: no category tree (fetched or cached) - the aisle rules were not applied.")
        return pages, notes
    plan = woolworths_shelves.plan_shelves(rules, tree_paths, taken_labels={p.label for p in pages})
    combined = pages + [BrowsePage(s.path, s.label, (), s.max_pages) for s in plan.shelves]
    keep, folded = woolworths_shelves.drop_covered([p.path for p in combined])
    by_path: dict[str, BrowsePage] = {}
    for page in combined:
        by_path.setdefault(page.path, page)
    for path, wider in folded:
        if wider == path:
            continue
        target = by_path[wider]
        merged = tuple(dict.fromkeys(target.covers + by_path[path].covers))
        by_path[wider] = BrowsePage(target.path, target.label, merged, target.max_pages)
    final = [by_path[combined[i].path] for i in keep]
    notes.append(
        f"Woolworths shelves: {len(plan.shelves)} aisle(s) from the category tree, "
        f"{len(folded)} listed shelf(ves) folded into them, {len(final)} browse pages in all."
    )
    if plan.promotional_skipped:
        notes.append(f"  promotional aisle(s) left out: {', '.join(plan.promotional_skipped)}")
    if plan.missing:
        notes.append(f"  named in the rules but not in the tree (skipped): {', '.join(plan.missing)}")
    return final, notes


def _keep_valid(products: list[dict], dropped: list[str]) -> list[dict]:
    kept = []
    for product in products:
        valid, reason = is_valid_product(product)
        if valid:
            kept.append(product)
        else:
            dropped.append(f"{product['product_id']} {product['name']} - {reason}")
    return kept


def browse_signature(pages: list[BrowsePage]) -> str:
    return checkpoint.signature([f"{p.path}|{p.max_pages}" for p in pages])


def browse_all(pages: list[BrowsePage], progress: bool = False, scraped_at: str | None = None) -> dict:
    """Step 1: read every browse path. A failing path is logged and skipped.

    With `progress` (a saving run) the state is written after every shelf, and a same-day
    run of the same shelf list carries on after the last shelf finished.
    """
    products: list[dict] = []
    dropped: list[str] = []
    scraped: dict[str, tuple[str, ...]] = {}
    blocked: list[str] = []
    failed = 0
    page_stats: list[page_limits.PageStat] = []
    first = 0
    sig = browse_signature(pages)
    saved = checkpoint.load_section(STORE, "browse", sig) if progress else None
    if saved is not None:
        try:
            first = int(saved["done"])
            if not 0 < first <= len(pages):
                raise ValueError("done out of range")
            products, dropped, blocked = list(saved["products"]), list(saved["dropped"]), list(saved["blocked"])
            failed = int(saved["failed"])
            scraped = {k: tuple(v) for k, v in saved["scraped"].items()}
            page_stats = checkpoint.page_stats_from_records(saved["page_stats"])
            scraped_at = saved["scraped_at"]
            log(f"\nResuming the browse step after shelf {first} of {len(pages)} (saved earlier today).")
        except (KeyError, TypeError, ValueError, AttributeError):
            first, products, dropped, scraped, blocked, failed, page_stats = 0, [], [], {}, [], 0, []

    def save_progress(done: int) -> None:
        checkpoint.save_section(
            STORE,
            "browse",
            sig,
            {
                "done": done,
                "products": products,
                "dropped": dropped,
                "blocked": blocked,
                "failed": failed,
                "scraped": {k: list(v) for k, v in scraped.items()},
                "page_stats": [asdict(s) for s in page_stats],
                "scraped_at": scraped_at,
            },
        )

    remembered = page_limits.load_page_counts(STORE)
    for i, page in enumerate(pages, start=1):
        if i <= first:
            continue
        log(f"\n[{i}/{len(pages)}] browsing: {page.path}")
        limit = max(page.max_pages, remembered.get(page.path, 0))
        stat = page_limits.PageStat(label=page.label, path=page.path, configured=page.max_pages, limit=limit)
        try:
            found, skip_reasons, total = browse_products(page.path, limit, page.label, stat=stat)
        except RobotsDisallowed as e:
            log(f"  SKIPPED: robots.txt disallows {e}")
            failed += 1
            if progress:
                save_progress(i)
            continue
        except BotDetected as e:
            log(f"  BLOCKED: {e}")
            blocked.append(f"{page.path} - {e}")
            failed += 1
            if progress:
                save_progress(i)
            continue
        except Exception as e:
            log(f"  FAILED: {type(e).__name__}: {e}")
            failed += 1
            if progress:
                save_progress(i)
            continue
        page_stats.append(stat)
        page_limits.log_cut_short([stat], log)
        log(f"  {len(found)} of {total} product(s) read, {len(skip_reasons)} skipped")
        dropped.extend(f"[{page.path}] {r}" for r in skip_reasons)
        shelf = _keep_valid(found, dropped)
        shelf, _ = cooking_wine.select_for_shelf(page.path, shelf, log)
        products.extend(shelf)
        scraped.setdefault(page.label, page.covers)
        if progress:
            save_progress(i)
    products = dedupe_products(products)
    counts = page_limits.cut_short_counts(page_stats)
    stats = {
        "categories_cut_short": counts["cut_short"],
        "extra_pages_read": counts["extra_pages"],
        "categories_scraped": len(scraped),
        "categories_failed": failed,
        "terms_blocked": blocked,
        "products_dropped": dropped,
    }
    return {
        "products": products,
        "scraped_categories": scraped,
        "stats": stats,
        "page_stats": page_stats,
        "scraped_at": scraped_at,
    }


SEARCH_SAVE_EVERY = 20  # terms between progress saves


def search_all(
    terms: list[str],
    size: int,
    deadline: float | None = None,
    collected: list[dict] | None = None,
    progress: bool = False,
) -> tuple[SearchOutcome, list[str]]:
    """Step 2: look each term up through the product-search API.

    A term is found when at least one result shares a word with it (the API is
    fuzzy and returns loose matches). It is not-found only when the search ran
    and nothing sensible came back. A robots refusal or a failed request leaves
    the term unreported; a 403/429 stops the whole step.

    Matching here is the local matcher, per term. `collected`, if given, gets every
    valid product any search returned, so the app's matcher can be run over them all.

    With `progress` (a saving run) the state is written every SEARCH_SAVE_EVERY terms and at
    the end, and a same-day run of the same term list carries on after the last saved term.
    The search time already used by the earlier run comes off the time budget (`deadline`).
    """
    outcome = SearchOutcome()
    blocked: list[str] = []
    first = 0
    before = 0.0
    sig = checkpoint.signature(terms)
    saved = checkpoint.load_section(STORE, "search", sig) if progress else None
    if saved is not None:
        try:
            done = int(saved["done"])
            if not 0 < done <= len(terms):
                raise ValueError("done out of range")
            restored = SearchOutcome(
                searched=list(saved["searched"]),
                found={k: list(v) for k, v in saved["found"].items()},
                not_found=list(saved["not_found"]),
                unreported=list(saved["unreported"]),
            )
            restored_blocked, restored_collected, before = list(saved["blocked"]), list(saved["collected"]), float(saved["spent"])
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        else:
            first, outcome, blocked = done, restored, restored_blocked
            if collected is not None:
                collected.extend(restored_collected)
            if deadline is not None:
                deadline -= before
            log(f"Resuming the search step after term {first} of {len(terms)} (saved earlier today).")
    began = time.monotonic()

    def save_progress(done: int) -> None:
        checkpoint.save_section(
            STORE,
            "search",
            sig,
            {
                "done": done,
                "searched": outcome.searched,
                "found": outcome.found,
                "not_found": outcome.not_found,
                "unreported": outcome.unreported,
                "blocked": blocked,
                "collected": dedupe_products(collected) if collected else [],
                "spent": before + time.monotonic() - began,
            },
        )

    for index, term in enumerate(terms):
        if index < first:
            continue
        if index > first and progress and (index - first) % SEARCH_SAVE_EVERY == 0:
            save_progress(index)
        if deadline is not None and time.monotonic() > deadline:
            outcome.unsearched = terms[index:]
            log(f"Search step ran out of time - {len(outcome.unsearched)} term(s) not searched.")
            break
        try:
            results, _ = search_products(term, size=size)
        except RobotsDisallowed as e:
            log(f"  [{term}] SKIPPED: robots.txt disallows {e}")
            outcome.searched.append(term)
            outcome.unreported.append(term)
            continue
        except BotDetected as e:
            log(f"  [{term}] BLOCKED: {e} - stopping the search step.")
            blocked.append(f"{term} - {e}")
            outcome.unsearched = terms[index:]
            break
        except Exception as e:
            log(f"  [{term}] search failed ({type(e).__name__}: {e}) - not reported either way.")
            outcome.searched.append(term)
            outcome.unreported.append(term)
            continue
        outcome.searched.append(term)
        valid = [p for p in results if is_valid_product(p)[0]]
        if collected is not None:
            collected.extend(valid)
        matches = match_products(term, valid)
        if matches:
            outcome.found[term] = matches
        else:
            outcome.not_found.append(term)
    if progress and not blocked:
        save_progress(len(terms) - len(outcome.unsearched))
    return outcome, blocked


def apply_app_matcher(
    outcome: SearchOutcome, products: list[dict], rules_version: str | None = None
) -> SearchOutcome:
    """Re-decide found / not-found for the searched terms with the app's matcher.

    products are the browse products plus everything the searches returned. A term
    is "not found" only if its own search ran (it is not in outcome.unreported);
    if the app can't be used the local per-term outcome is kept unchanged.
    """
    products = dedupe_products(products)
    matches, reason = try_app_match(STORE, outcome.searched, products, rules_version)
    if matches is None:
        if outcome.searched:
            outcome.matcher, outcome.fallback_reason = "local fallback", reason
        return outcome
    search_failed = set(outcome.unreported)
    rebuilt = outcome_from_app(matches, outcome.searched, products, lambda t: t not in search_failed)
    rebuilt.skipped, rebuilt.unsearched = outcome.skipped, outcome.unsearched
    return rebuilt


_FETCH_LATER = object()


def run_search_step(
    args, browse_products_list: list[dict] | None = None, lists=_FETCH_LATER
) -> tuple[SearchOutcome, list[str]]:
    """`lists` is the list already fetched at the start of the run (None if the app
    couldn't be reached); by default it is fetched here."""
    deadline = time.monotonic() + SEARCH_TIME_BUDGET_SECONDS
    if lists is _FETCH_LATER:
        lists = fetch_store_lists(STORE, args.ingredient_list_url)
    reportable = lists is not None
    skipped: list[str] = []
    if lists is None:
        terms = parse_terms_file(args.terms)
        log(f"App list unavailable - falling back to {len(terms)} term(s) from {args.terms}; nothing will be reported.")
    else:
        terms, skipped = plan_terms(lists)
        log(
            f"App list for {STORE}: {len(lists.search_terms)} search term(s), {len(lists.watch_terms)} watch "
            f"term(s), {len(skipped)} skipped."
        )
    log(f"{len(terms)} term(s) to search.")
    collected: list[dict] = []
    outcome, blocked = search_all(terms, args.size, deadline, collected, progress=args.save)
    outcome.skipped = skipped
    outcome = apply_app_matcher(
        outcome, list(browse_products_list or []) + collected, lists.match_rules_version if lists else None
    )
    log_outcome(outcome)
    if not reportable:
        pass
    elif args.save:
        report_outcome(STORE, outcome)
    else:
        log("Dry run - would report to the app:")
        log(f"  found     : {json.dumps({'terms': list(outcome.found)})}")
        log(f"  not-found : {json.dumps({'terms': outcome.not_found, 'searchedAt': '<now>'})}")
    return outcome, blocked


def print_table(products: list[dict]) -> None:
    print()
    header = f"{'ID':>10} | {'Name':<40} | {'Size':<10} | {'Price':>7} | {'Unit Price'}"
    print(header)
    print("-" * len(header))
    for p in products:
        name = (p["name"] or "")[:40]
        size = p["size"] or ""
        price = f"${p['price']:.2f}" if p["price"] is not None else ""
        if p["unit_price"] is not None:
            unit_price = f"${p['unit_price']:.2f}/{p['unit']}"
        else:
            unit_price = ""
        print(f"{p['product_id']:>10} | {name:<40} | {size:<10} | {price:>7} | {unit_price}")
    print()


def print_summary(stats: dict) -> None:
    print("=" * 60)
    print("WOOLWORTHS RUN SUMMARY")
    print("=" * 60)
    print(f"Browse paths read           : {stats['categories_scraped']}")
    print(f"Browse paths failed         : {stats['categories_failed']}")
    print(f"Categories cut short        : {stats.get('categories_cut_short', 0)}")
    print(f"Extra pages read            : {stats.get('extra_pages_read', 0)}")
    print(f"Requests blocked (403/429)  : {len(stats['terms_blocked'])}")
    for reason in stats["terms_blocked"]:
        print(f"  - {reason}")
    print(f"Products saved              : {stats['products_saved']}")
    print(f"  - with a unit price       : {stats['products_with_unit_price']}")
    print(f"  - without a unit price    : {stats['products_without_unit_price']}")
    print(f"Products dropped entirely   : {len(stats['products_dropped'])}")
    for reason in stats["products_dropped"]:
        print(f"  - {reason}")
    print("=" * 60)


def _enrich_sheets(products: list[dict], save: bool) -> None:
    """Best-effort sheet counts from product descriptions; never stops the run."""
    try:
        lookups = enrich_sheet_counts(
            products, get_product_description_text, save_cache=save, log=log
        )
        if lookups:
            log(f"Sheet counts: looked up {lookups} product description(s).")
    except BotDetected:
        log("Sheet-count lookups stopped - the store pushed back.")
    except Exception as e:
        log(f"Sheet-count lookups skipped ({type(e).__name__}: {e}).")


def main() -> int:
    return lock.run_locked("woolworths scrape", run, log)


def run() -> int:
    started = time.monotonic()
    args = parse_args()
    power_start = power.log_power("at start", log)
    if args.save and checkpoint.exists_today(STORE):
        log("Found progress saved earlier today by a run that was cut off - carrying on from it.")
    if not args.save:
        log("(Dry Run Mode - nothing is written, published or reported. Pass --save to do it for real.)")

    log("\nSTEP 0: CATEGORY TREE AND WATCH CATEGORIES")
    lists = _FETCH_LATER
    try:
        lists = fetch_store_lists(STORE, args.ingredient_list_url)
    except Exception as e:
        log(f"Could not fetch the app's list up front ({type(e).__name__}) - the search step will try again.")
    mapping: MappingResult | None = None
    pages = []
    tree: list = []
    try:
        pages = parse_categories_file(args.categories)
        mapping = sync_and_map(STORE, None if lists is _FETCH_LATER else lists, save=args.save, tree_out=tree)
        if mapping:
            extra = extra_browse_pages(mapping.mapped, pages)
            log(f"{len(extra)} mapped watch categor(ies) to browse beyond the categories file.")
            pages = pages + extra
    except Exception as e:
        log(f"Watch category step failed ({type(e).__name__}: {e}) - carrying on without it.")
    try:
        pages, notes = planned_pages(args.categories, [c.path for c in tree], pages)
        for note in notes:
            log(note)
    except Exception as e:
        log(f"Main shelves could not be planned ({type(e).__name__}: {e}) - browsing the listed shelves only.")

    log("\nSTEP 1: BROWSE (watch categories)")
    scraped_at = now_nz_iso()
    try:
        browse = browse_all(pages, progress=args.save, scraped_at=scraped_at)
    except Exception as e:
        log(f"UNEXPECTED ERROR - stopping run: {e!r}")
        return 1
    scraped_at = browse.get("scraped_at") or scraped_at
    browse_products_list = browse["products"]
    stats = browse["stats"]

    _enrich_sheets(browse_products_list, args.save)

    if args.save and browse_products_list:
        today = date.today().isoformat()
        snapshot_path = write_snapshot(browse_products_list, args.snapshots_dir, today)
        write_scrape_meta(snapshot_path, scraped_at)
        page_limits.save_page_stats(STORE, browse.get("page_stats", []), log)
        _, new_entries = update_price_history(browse_products_list, args.history_path)
        log(f"Snapshot written to {snapshot_path}")
        log(f"Price history updated: {new_entries} new entr{'y' if new_entries == 1 else 'ies'} added")

    log("\nSTEP 2: SEARCH (product-search API)")
    outcome = SearchOutcome()
    try:
        outcome, blocked = run_search_step(args, browse_products_list, lists)
        stats["terms_blocked"] = stats["terms_blocked"] + blocked
    except Exception as e:
        log(f"Search step failed ({type(e).__name__}: {e}) - carrying on to publish.")

    browse_ids = {product_key(p) for p in browse_products_list}
    search_only = [p for p in outcome.matched_products if product_key(p) not in browse_ids]
    _enrich_sheets(search_only, args.save)
    to_publish = dedupe_products(browse_products_list + outcome.matched_products)
    stats["products_saved"] = len(to_publish)
    stats["products_with_unit_price"] = sum(1 for p in to_publish if p["unit_price"] is not None)
    stats["products_without_unit_price"] = len(to_publish) - stats["products_with_unit_price"]

    log("\nSTEP 3: PUBLISH")
    print_table(to_publish)
    print_summary(stats)
    published = 0
    run_status = None
    if args.save:
        extra = search_only
        snapshot_final = True
        if extra or to_publish != browse_products_list:
            try:
                today = date.today().isoformat()
                write_scrape_meta(write_snapshot(to_publish, args.snapshots_dir, today), scraped_at)
                if extra:
                    update_price_history(extra, args.history_path)
            except Exception as e:
                snapshot_final = False
                log(f"Could not update snapshot/history with search results ({type(e).__name__}: {e}).")
        if snapshot_final:
            checkpoint.clear(STORE)
        try:
            snapshot = Path(args.snapshots_dir) / f"{date.today().isoformat()}.json"
            run = publish_run(
                to_publish,
                STORE,
                scraped_at,
                legacy=args.legacy_mailbox,
                snapshot_path=snapshot if snapshot.exists() else None,
            )
            published = run.items
            run_status = run.summary()
        except Exception as e:
            log(f"Publishing failed ({type(e).__name__}: {e}).")
    else:
        log("Dry run - nothing published.")

    print(f"Ingredients matched         : {len(outcome.found)}")
    power_end = power.log_power("at end", log)
    log(
        summary_line(
            published,
            outcome,
            stats["categories_scraped"],
            time.monotonic() - started,
            mapping.counts() if mapping else None,
            run_status,
            {"cut_short": stats.get("categories_cut_short", 0), "extra_pages": stats.get("extra_pages_read", 0)},
            power.summary_part(power_start, power_end),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
