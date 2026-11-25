"""The Woolworths main grocery shelves, built from the posted category tree.

woolworths_categories.txt names departments and the second-level shelves (aisles) to read
whole, with lines like

    @aisles fruit-veg fruit,vegetables,fresh-salad-herbs,prepared-fruit-veg [pages=N]
    @aisles pantry *

`*` means every aisle of the department. Each rule is looked up in the store's category tree
(the one posted to the app, refreshed at the start of every run); an aisle the tree no longer
has is reported and skipped, and a promotional aisle (3-for-20, fresh-deals, ... see
scraper/store_category.py) is never read. A shelf below an aisle that is read whole is not
read again on its own.

    python -m scraper.woolworths_shelves     # dry run: the shelf list, expected pages and items (no network)
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path

from scraper import page_limits
from scraper.category_tree import load_cache
from scraper.store_category import is_promotional

STORE = "woolworths"
RULE_PREFIX = "@aisles"
DEFAULT_RULE_PAGES = 5

# Expected-size model for the dry run only (the run itself reads until each shelf's total is covered).
BROWSE_PAGE_SIZE = 120
ITEMS_PER_LEAF_SHELF = 36  # fallback: median items per bottom-level shelf on the aisles read so far (9 to 51)
SECONDS_PER_REQUEST = 2.7  # request delay 2-3 s plus the round trip, from the 18 and 25 Nov logs


@dataclass(frozen=True)
class AisleRule:
    department: str
    aisles: tuple[str, ...] | None  # None: every aisle of the department
    max_pages: int = DEFAULT_RULE_PAGES


@dataclass(frozen=True)
class ShelfPlan:
    path: str
    label: str
    max_pages: int


@dataclass
class ShelfPlanResult:
    shelves: list[ShelfPlan]
    promotional_skipped: list[str]
    missing: list[str]  # named in a rule, not in the tree


def parse_rules(path: str | Path) -> list[AisleRule]:
    """The `@aisles` lines of the categories file. Other lines are for parse_categories_file."""
    path = Path(path)
    if not path.exists():
        return []
    rules: list[AisleRule] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line.startswith(RULE_PREFIX):
            continue
        parts = line.split()[1:]
        if len(parts) < 2:
            continue
        department, names = parts[0].strip("/"), parts[1]
        aisles = None if names == "*" else tuple(a.strip("/") for a in names.split(",") if a.strip("/"))
        max_pages = DEFAULT_RULE_PAGES
        for part in parts[2:]:
            if part.startswith("pages="):
                try:
                    max_pages = max(1, int(part.split("=", 1)[1]))
                except ValueError:
                    pass
        rules.append(AisleRule(department, aisles, max_pages))
    return rules


def _second_level(tree_paths) -> dict[str, list[str]]:
    by_department: dict[str, list[str]] = {}
    for path in sorted({p for p in tree_paths if p.count("/") == 1}):
        by_department.setdefault(path.split("/")[0], []).append(path)
    return by_department


def plan_shelves(rules: list[AisleRule], tree_paths, taken_labels=()) -> ShelfPlanResult:
    """The aisle paths the rules name that the tree has, in rule order, promotional ones left out.

    Labels (the shelf name in logs and in the scraped-categories list) are the aisle slug, made
    unique with the department when two aisles share it or `taken_labels` already has it.
    """
    tree_paths = set(tree_paths)
    by_department = _second_level(tree_paths)
    shelves: list[ShelfPlan] = []
    promotional: list[str] = []
    missing: list[str] = []
    seen_labels: set[str] = set(taken_labels)
    seen_paths: set[str] = set()
    for rule in rules:
        if rule.department not in by_department:
            missing.append(rule.department)
            continue
        if rule.aisles is None:
            wanted = by_department[rule.department]
        else:
            wanted = []
            for aisle in rule.aisles:
                path = f"{rule.department}/{aisle}"
                if path in tree_paths:
                    wanted.append(path)
                else:
                    missing.append(path)
        for path in wanted:
            if path in seen_paths:
                continue
            if is_promotional(path):
                promotional.append(path)
                continue
            seen_paths.add(path)
            aisle = path.split("/")[1]
            label = aisle if aisle not in seen_labels else f"{rule.department}-{aisle}"
            seen_labels.add(label)
            shelves.append(ShelfPlan(path, label, rule.max_pages))
    return ShelfPlanResult(shelves, promotional, missing)


def covered_by(path: str, others) -> str | None:
    """The path in `others` that contains `path` (a shelf below an aisle), if any."""
    return next((o for o in others if o != path and path.startswith(o + "/")), None)


def drop_covered(paths: list[str]) -> tuple[list[int], list[tuple[str, str]]]:
    """(indexes of `paths` to keep, [(dropped path, the wider path that reads it)]).

    A repeated path and a shelf inside another listed path are dropped; the first of two equal paths stays.
    """
    keep: list[int] = []
    dropped: list[tuple[str, str]] = []
    unique = list(dict.fromkeys(paths))
    for index, path in enumerate(paths):
        if paths.index(path) != index:
            dropped.append((path, path))
            continue
        wider = covered_by(path, unique)
        if wider:
            dropped.append((path, wider))
        else:
            keep.append(index)
    return keep, dropped


# --- dry run: expected pages and items --------------------------------------------------

def leaf_counts(tree_paths) -> dict[str, int]:
    """Bottom-level shelves under each path (a path with no children counts as one)."""
    paths = sorted(set(tree_paths))
    parents = {p.rsplit("/", 1)[0] for p in paths if "/" in p}
    counts: dict[str, int] = {}
    for path in paths:
        prefix = path + "/"
        counts[path] = sum(1 for p in paths if p.startswith(prefix) and p not in parents) or 1
    return counts


def known_items(store: str = STORE) -> dict[str, int]:
    """Items per path actually read on the last real run, from data/page_stats."""
    try:
        files = sorted(page_limits.stats_path(store).parent.glob("*.json"))
        import json

        data = json.loads(files[-1].read_text())
        return {c["path"]: int(c["items"]) for c in data.get("categories", []) if c.get("items")}
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return {}


@dataclass(frozen=True)
class Expectation:
    path: str
    items: int
    pages: int
    basis: str  # "last run" or "estimate"


def expect(paths: list[str], tree_paths, known: dict[str, int]) -> list[Expectation]:
    leaves = leaf_counts(tree_paths)
    remembered = page_limits.load_page_counts(STORE)
    ratios = sorted(known[p] / leaves[p] for p in known if p in leaves and p.count("/") == 1)
    per_leaf = ratios[len(ratios) // 2] if ratios else ITEMS_PER_LEAF_SHELF
    out = []
    for path in paths:
        if path in known:
            items, basis = known[path], "last run"
        else:
            items, basis = round(leaves.get(path, 1) * per_leaf), "estimate"
        pages = max(1, math.ceil(items / BROWSE_PAGE_SIZE), remembered.get(path, 0))
        out.append(Expectation(path, items, pages, basis))
    return out


def main(argv: list[str] | None = None) -> int:
    from scraper.woolworths import DEFAULT_CATEGORIES_PATH, parse_categories_file, planned_pages

    cached = load_cache(STORE)
    if not cached:
        print("No cached Woolworths category tree (data/category_trees/woolworths.json) - nothing to list.")
        return 1
    tree_paths = [c.path for c in cached["categories"]]
    pages, notes = planned_pages(DEFAULT_CATEGORIES_PATH, tree_paths, parse_categories_file(DEFAULT_CATEGORIES_PATH))
    expectations = expect([p.path for p in pages], tree_paths, known_items())
    print(f"Woolworths shelf list from the cached tree ({cached.get('updatedAt', '?')}): {len(pages)} shelves\n")
    print(f"{'#':>3}  {'shelf':<52} {'pages':>5} {'items':>6}  basis")
    for number, (page, e) in enumerate(zip(pages, expectations), start=1):
        print(f"{number:>3}  {page.path:<52} {e.pages:>5} {e.items:>6}  {e.basis}")
    total_pages, total_items = sum(e.pages for e in expectations), sum(e.items for e in expectations)
    estimated = sum(1 for e in expectations if e.basis == "estimate")
    print(f"\nTotal: {total_pages} page requests, about {total_items} items ({estimated} of {len(pages)} shelves are estimates).")
    for line in notes:
        print(line)
    minutes = total_pages * SECONDS_PER_REQUEST / 60
    print(f"Browse time at {SECONDS_PER_REQUEST:g} s a request: about {minutes:.0f} minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
