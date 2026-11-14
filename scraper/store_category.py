"""The store's own category path for a product, in the same form as the posted trees.

  Pak'nSave / New World: the path of the category page the product was scraped
    from ("pantry/oil--vinegar/cooking-oil" - the part of the URL after /shop/category/).
  Woolworths: department/aisle/shelf ("pantry/baking/flour"), worked out from the
    product's own department / aisle / shelf names in the API and matched to the
    cached tree, or the browse path the product was read from.

Blank means unknown; a path is never guessed. A product seen on several pages keeps
a real (non-promotional) path first, deepest, then the first one seen; a promotional
path (see PROMO_*) is kept only when it is the only one.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREE_DIR = os.path.join(REPO_ROOT, "data", "category_trees")
FOODSTUFFS_MARKER = "/shop/category/"

_tree_cache: dict[tuple[str, str], "Tree"] = {}


# --- promotional paths (the one place to extend) -------------------------------

PROMO_ROOTS = {"featured", "christmas"}  # first level: everything below is a promotion
PROMO_SECOND_LEVELS = {"3-for-20", "3-for-12-deli", "fresh-deals", "in-season"}  # exact names, at any level below the first
PROMO_PATTERNS = (  # regexes tried on every level below the first, e.g. "2-for-5", "weekly-specials"
    re.compile(r"(?:^|-)\d+-for-\d+(?:-|$)"),
    re.compile(r"(?:^|-)(?:deals?|specials?|clearance)(?:-|$)"),
)


def is_promotional(path: str) -> bool:
    """True for a promotion path (featured/..., christmas/..., 3-for-20, fresh-deals, ...), not a real department."""
    parts = [p for p in (path or "").lower().strip("/").split("/") if p]
    if not parts:
        return False
    if parts[0] in PROMO_ROOTS:
        return True
    return any(part in PROMO_SECOND_LEVELS or any(rx.search(part) for rx in PROMO_PATTERNS) for part in parts[1:])


def depth(path: str) -> int:
    return len(path.strip("/").split("/")) if path and path.strip("/") else 0


def deeper(current: str, candidate: str) -> str:
    """candidate if it is strictly deeper than current, else current (so the first seen wins a tie)."""
    return candidate if depth(candidate) > depth(current) else current


def better_category(current: str, candidate: str) -> str:
    """The category to keep when a product is seen with another path.

    A real path beats a promotional one; among the same kind the deeper wins, then the first seen.
    """
    if not candidate:
        return current
    if not current:
        return candidate
    if is_promotional(current) != is_promotional(candidate):
        return candidate if is_promotional(current) else current
    return deeper(current, candidate)


def foodstuffs_path_of(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    return path.split(FOODSTUFFS_MARKER, 1)[1].strip("/") if FOODSTUFFS_MARKER in path else path.strip("/")


# --- the cached tree -------------------------------------------------------

class Tree:
    def __init__(self, categories: list[dict]):
        self.names = {c["path"]: c["name"] for c in categories}
        self.children: dict[str, list[str]] = {}
        for c in categories:
            self.children.setdefault(c.get("parent", ""), []).append(c["path"])

    def __bool__(self) -> bool:
        return bool(self.names)


def load_tree(store: str) -> Tree:
    """The store's cached category tree (empty if there is none). Cached per file."""
    path = Path(TREE_DIR) / f"{store}.json"
    key = (store, str(path))
    if key not in _tree_cache:
        try:
            _tree_cache[key] = Tree(json.loads(path.read_text())["categories"])
        except (OSError, ValueError, KeyError, TypeError):
            _tree_cache[key] = Tree([])
    return _tree_cache[key]


def clear_tree_cache() -> None:
    _tree_cache.clear()


# --- Woolworths ------------------------------------------------------------

def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower().replace("&", " and ")).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def _first_name(value) -> str:
    """The name of the first entry of an API list like [{"id": 1, "name": "Pantry"}]."""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return ""
    for entry in value:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def _child_named(tree: Tree, parent: str, name: str) -> str:
    wanted, slug = _plain(name), _slug(name)
    for child in tree.children.get(parent, []):
        if _plain(tree.names[child]) == wanted or child.rsplit("/", 1)[-1] == slug:
            return child
    return ""


def woolworths_own_path(item: dict, tree: Tree) -> str:
    """The product's own department/aisle/shelf as a tree path; stops at the last level the tree confirms."""
    if not tree:
        return ""
    path = ""
    for key in ("departments", "aisles", "shelves"):
        name = _first_name(item.get(key))
        child = _child_named(tree, path, name) if name else ""
        if not child:
            break
        path = child
    return path


def woolworths_category(item: dict, browse_path: str | None = None, tree: Tree | None = None) -> str:
    """The better of the product's own path and the browse path it was read from ("" if neither is known)."""
    own = woolworths_own_path(item, load_tree("woolworths") if tree is None else tree)
    return better_category(own, (browse_path or "").strip("/"))


# --- merging ---------------------------------------------------------------

def merge_category(kept: dict, other: dict) -> dict:
    """kept, with other's store_category if that is better (see better_category). Copies kept only when it has to change."""
    better = better_category(kept.get("store_category") or "", other.get("store_category") or "")
    if better and better != (kept.get("store_category") or ""):
        return {**kept, "store_category": better}
    return kept
