"""Map the app's watch categories onto each store's own categories.

A watch category is either a plain name ("baking") or an object with a name and a
path per store. For one store:
  1. a path the app gave for this store is used as it is;
  2. otherwise (blank path, or a plain name) the store's cached category tree is
     searched by name: exact match, then normalised match (case, "&" / "and",
     plurals, "canned" / "tinned"), then closest by words;
  3. nothing found: one log line, and the category is skipped at this store.

A wrong mapping is worse than none, so the word match is conservative: every word
of one name must appear in the other, and the closest (fewest extra words, then
shallowest) wins. Mappings are kept in data/category_maps/<store>.json so the next
run can tell what is newly mapped.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scraper import category_tree
from scraper.browser import CategoryPage
from scraper.category_tree import Category, log
from scraper.ingredients import WatchCategory
from scraper.matching import _stem

# Page limit for a mapped watch category at Pak'nSave / New World: the same limit
# Woolworths uses for its main lists (DEFAULT_BROWSE_MAX_PAGES), above the 3 pages
# the largest categories file entry asks for. Paging stops earlier at the last page.
MAPPED_FOODSTUFFS_MAX_PAGES = 5

_STOPWORDS = {"and", "the", "of", "a", "for", "in", "with", "food", "product", "supply"}
# Extra words that turn a category into something else ("Dairy Free & Meat Free" is not "dairy").
_NEGATING = {"free", "less", "without", "non"}
# Promotional trees duplicate real categories under another path.
_PROMO_ROOTS = {"featured"}
_WORD_SYNONYMS = {"canned": "tinned", "can": "tin", "cans": "tin", "tins": "tin"}


def _words(text: str) -> list[str]:
    text = text.lower().replace("&", " and ")
    out = []
    for raw in re.findall(r"[a-z0-9]+", text):
        word = _WORD_SYNONYMS.get(raw, raw)
        word = _stem(word)
        if len(word) > 4 and word.endswith("ing"):
            word = word[:-3]
        elif len(word) > 4 and word.endswith("er"):
            word = word[:-2]
        word = _WORD_SYNONYMS.get(word, word)
        out.append(word)
    return out


def _significant(text: str) -> frozenset[str]:
    return frozenset(w for w in _words(text) if w not in _STOPWORDS)


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower().replace("&", " and ")).strip()


@dataclass(frozen=True)
class Mapping:
    name: str
    path: str
    how: str  # "given", "exact", "normalised", "words" or "remembered"
    new: bool = False


@dataclass
class MappingResult:
    store: str
    mapped: list[Mapping]
    not_found: list[str]

    @property
    def newly_mapped(self) -> int:
        return sum(1 for m in self.mapped if m.new)

    def counts(self) -> dict:
        return {"mapped": len(self.mapped), "new": self.newly_mapped, "not_found": len(self.not_found)}


def clean_path(path: str) -> str:
    """A given path as a bare category path: no site, no /shop/category/ or /shop/browse/ prefix."""
    path = urllib.parse.unquote((path or "").strip())
    if "://" in path:
        path = urllib.parse.urlsplit(path).path
    path = path.split("?")[0].strip("/")
    for prefix in ("shop/category/", "shop/browse/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    return path.strip("/")


def find_by_name(name: str, categories: list[Category]) -> tuple[Category, str] | None:
    """The store category best matching this name, with how it matched, or None."""
    if not name.strip() or not categories:
        return None

    def depth(c: Category) -> int:
        return c.path.count("/")

    ordered = [w for w in _words(name) if w not in _STOPWORDS]

    def context(c: Category) -> int:
        """Lower when the aisle above holds the wanted name's first word ("kitchen" for "kitchen cleaning")."""
        above = _significant(c.parent.replace("/", " ").replace("-", " "))
        return next((i for i, w in enumerate(ordered) if w in above), len(ordered))

    def pick(candidates: list[Category]) -> Category:
        return min(candidates, key=lambda c: (c.path.split("/")[0] in _PROMO_ROOTS, depth(c), context(c), c.path))

    wanted_plain = _plain(name)
    exact = [c for c in categories if c.name.lower() == name.strip().lower()]
    if exact:
        return pick(exact), "exact"

    plain = [c for c in categories if _plain(c.name) == wanted_plain]
    if plain:
        return pick(plain), "normalised"

    wanted = _significant(name)
    if not wanted:
        return None
    normalised = [c for c in categories if _significant(c.name) == wanted]
    if normalised:
        return pick(normalised), "normalised"

    best: tuple[tuple, Category] | None = None
    for c in categories:
        words = _significant(c.name)
        if not words:
            continue
        if wanted <= words:
            if words - wanted & _NEGATING:
                continue
            extra = len(words - wanted)
        elif words <= wanted and len(words) >= 1 and len(words) * 2 > len(wanted):
            extra = len(wanted - words) + 1
        else:
            continue
        key = (c.path.split("/")[0] in _PROMO_ROOTS, extra, depth(c), context(c), c.path)
        if best is None or key < best[0]:
            best = (key, c)
    if best:
        return best[1], "words"
    return None


# --- stored mappings -----------------------------------------------------

def mapping_path(store: str) -> Path:
    return Path(category_tree.STATE_DIR) / "category_maps" / f"{store}.json"


def load_mappings(store: str) -> dict[str, dict]:
    try:
        data = json.loads(mapping_path(store).read_text())
        return {k: v for k, v in data.get("mappings", {}).items() if isinstance(v, dict) and v.get("path")}
    except (OSError, ValueError, AttributeError):
        return {}


def save_mappings(store: str, mappings: dict[str, dict]) -> None:
    path = mapping_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"store": store, "mappings": mappings}, indent=1, sort_keys=True))
    tmp.replace(path)


# --- mapping -------------------------------------------------------------

def map_watch_categories(
    store: str, watch: list[WatchCategory], categories: list[Category], *, save: bool = True
) -> MappingResult:
    """Map every watch category for this store. Never raises.

    `categories` is the store's tree (live or cached). With save=False the
    per-store mapping file is read but not written (dry run).
    """
    previous = load_mappings(store)
    known_paths = {c.path for c in categories}
    by_path = {c.path: c for c in categories}
    mapped: list[Mapping] = []
    not_found: list[str] = []
    record: dict[str, dict] = dict(previous)
    seen: set[str] = set()

    for entry in watch:
        key = entry.name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)

        given = clean_path(entry.path_for(store))
        if given:
            path, how = given, "given"
        else:
            remembered = previous.get(key)
            if remembered and (not known_paths or remembered["path"] in known_paths) and remembered.get("how") != "given":
                path, how = remembered["path"], "remembered"
            else:
                found = find_by_name(entry.name, categories)
                if not found:
                    log(f"watch category '{entry.name}' not found at {store}")
                    not_found.append(entry.name)
                    record.pop(key, None)
                    continue
                path, how = found[0].path, found[1]

        is_new = key not in previous or previous[key].get("path") != path
        mapped.append(Mapping(entry.name, path, how, is_new))
        if how == "remembered":
            continue
        record[key] = {
            "name": entry.name,
            "path": path,
            "how": how,
            "treeName": by_path[path].name if path in by_path else None,
            "mappedAt": previous[key]["mappedAt"] if not is_new and "mappedAt" in previous.get(key, {}) else datetime.now(timezone.utc).isoformat(),
        }

    if save:
        try:
            save_mappings(store, record)
        except OSError as e:
            log(f"Could not save the {store} category mapping ({type(e).__name__}).")
    return MappingResult(store, mapped, not_found)


def log_mapping(result: MappingResult) -> None:
    c = result.counts()
    log(
        f"Watch categories at {result.store}: {c['mapped']} mapped ({c['new']} newly mapped), "
        f"{c['not_found']} not found."
    )
    for m in result.mapped:
        tag = " (new)" if m.new else ""
        log(f"  '{m.name}' -> {m.path} [{m.how}]{tag}")


# --- turning mappings into pages to scrape --------------------------------

def foodstuffs_path_of(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    marker = "/shop/category/"
    return path.split(marker, 1)[1].strip("/") if marker in path else path.strip("/")


def label_for(m: Mapping) -> str:
    return re.sub(r"[^a-z0-9]+", "-", m.name.lower()).strip("-") or m.path.rsplit("/", 1)[-1]


def covers_for(m: Mapping) -> tuple[str, ...]:
    return tuple(dict.fromkeys(w for w in re.findall(r"[a-z0-9]+", m.name.lower()) if w not in _STOPWORDS))


def extra_foodstuffs_pages(store: str, mapped: list[Mapping], existing: list[CategoryPage]) -> list[CategoryPage]:
    """Pages for mapped watch categories, leaving out any the categories file already scrapes."""
    from scraper.category_tree import _site_root

    have = {foodstuffs_path_of(p.url) for p in existing}
    pages = []
    for m in mapped:
        if m.path in have:
            continue
        have.add(m.path)
        url = f"{_site_root(store)}/shop/category/{urllib.parse.quote(m.path, safe='/_-')}"
        pages.append(
            CategoryPage(
                url=url, category=label_for(m), covers=covers_for(m), max_pages=MAPPED_FOODSTUFFS_MAX_PAGES
            )
        )
    return pages


def sync_and_map(store: str, lists, *, save: bool, guard=None, sync=None) -> MappingResult | None:
    """Sync the store's category tree (post if changed), then map its watch categories.

    `lists` is the app's StoreLists for this store (None if the app couldn't be
    reached). Returns None when there are no lists to map. Never raises - a
    failure here must not stop the scrape.
    """
    try:
        synced = (sync or category_tree.sync_tree)(store, save=save, guard=guard)
    except Exception as e:
        log(f"Category tree ({store}) failed ({type(e).__name__}: {e}) - carrying on without it.")
        return None
    if lists is None:
        log(f"Watch categories at {store}: the app's list is not available - nothing mapped.")
        return None
    try:
        result = map_watch_categories(store, lists.watch_category_specs(), synced.categories, save=save)
        log_mapping(result)
        return result
    except Exception as e:
        log(f"Watch category mapping ({store}) failed ({type(e).__name__}: {e}) - carrying on without it.")
        return None
