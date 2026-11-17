"""Cheap cooking wine: keep the cheapest white and red bottles per store.

The app prices recipe lines like "dry white wine" or "shiraz" from the cheapest white
or red wine at each store, per litre. Reading every bottle on the shelf is wasteful,
so for the white-wine and red-wine shelves only the cheapest few per shelf are kept.
The cask wine shelf is not trimmed: every cask is kept.

Ranking uses the public shelf price. A Club / member price is never used: for a member
promotion the regular price is ranked instead.
"""

from __future__ import annotations

import re

from scraper.unit_price import derive_from_size_and_price, parse_raw_unit_price

KEEP_PER_COLOUR = 5
WINE_ROOTS = {"beer-wine-and-cider", "beer-wine"}
COLOUR_SHELVES = {"white-wine": "white", "red-wine": "red"}

# Words that mark a wine as not a plain still table wine.
_EXCLUDED = re.compile(
    r"\b(sparkling|bubbly|brut|champagne|prosecco|cava|spumante|frizzante|fizz|"
    r"fortified|port|sherry|vermouth|marsala|madeira|"
    r"flavou?red|spritzer?|sangria|mulled|"
    r"dessert|late\s+harvest|ice\s*wine|moscato|"
    r"non[\s-]?alcoholic|alcohol[\s-]?free|de[\s-]?alcoholised|dealcoholised|zero|"
    r"gift|giftpack|hamper|mixed\s+case)\b|\b0(?:\.0)?\s*%",
    re.IGNORECASE,
)


def _parts(path: str) -> list[str]:
    return [p for p in (path or "").lower().strip("/").split("/") if p]


def wine_colour(path: str) -> str | None:
    """"white" or "red" for the white-wine / red-wine shelves (or anything below), else None.

    The cask wine shelf returns None: it is kept whole.
    """
    parts = _parts(path)
    if len(parts) >= 2 and parts[0] in WINE_ROOTS:
        return COLOUR_SHELVES.get(parts[1])
    return None


def is_cask_shelf(path: str) -> bool:
    parts = _parts(path)
    return len(parts) >= 2 and parts[0] in WINE_ROOTS and parts[1] == "cask-wine"


def excluded_reason(name: str) -> str | None:
    """The word that rules this wine out (sparkling, fortified, ...), or None."""
    match = _EXCLUDED.search(name or "")
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(0).lower())


def _regular_price(product: dict) -> float | None:
    if product.get("promo_type") == "member":
        regular = product.get("regular_pack_price")
        if regular is not None:
            return float(regular)
    price = product.get("price")
    return float(price) if price is not None else None


def price_per_litre(product: dict) -> float | None:
    """Regular (non-member) price per litre, or None when it cannot be worked out."""
    member = product.get("promo_type") == "member"
    unit_price, unit = product.get("unit_price"), product.get("unit")
    if not member and unit == "L" and unit_price is not None:
        return float(unit_price)
    if member:
        parsed = parse_raw_unit_price(product.get("unit_raw") or "", product.get("name"))
        if parsed is not None and parsed.unit == "L":
            return parsed.amount
    price, size = _regular_price(product), product.get("size")
    if price is not None and size:
        derived = derive_from_size_and_price(str(size), price)
        if derived is not None and derived.unit == "L":
            return derived.amount
    return None


def cheapest_wines(products: list[dict], count: int = KEEP_PER_COLOUR) -> tuple[list[dict], list[str], list[dict]]:
    """The cheapest `count` still wines by price per litre.

    Returns (kept products, notes on what was ruled out, ranked report rows). A product
    seen twice (same product_id) counts once. Wines with no usable price per litre are
    left out, since they cannot be compared.
    """
    notes: list[str] = []
    candidates: list[tuple[float, float, str, dict]] = []
    seen: set[str] = set()
    for product in products:
        pid = str(product.get("product_id", ""))
        if pid in seen:
            continue
        seen.add(pid)
        name = product.get("name", "")
        reason = excluded_reason(name)
        if reason:
            notes.append(f"{name} - ruled out ({reason})")
            continue
        per_litre = price_per_litre(product)
        if per_litre is None:
            notes.append(f"{name} - no price per litre")
            continue
        candidates.append((per_litre, _regular_price(product) or 0.0, name.lower(), product))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    chosen = candidates[:count]
    report = [
        {"name": p["name"], "price": price, "per_litre": per_litre, "size": p.get("size"), "product_id": p.get("product_id")}
        for per_litre, price, _, p in chosen
    ]
    return [c[3] for c in chosen], notes, report


def is_wine_shelf(path: str) -> bool:
    return wine_colour(path) is not None or is_cask_shelf(path)


def fill_unit_price(product: dict) -> bool:
    """Give a wine the per-litre price its tile does not show, worked out from its size.

    Pak'nSave and New World show no unit price on wine tiles. Only a wine with neither a unit
    price nor unit text is touched; the store's own figure is never replaced. Returns True if filled.
    """
    if product.get("unit_price") is not None or product.get("unit_raw"):
        return False
    price, size = _regular_price(product), product.get("size")
    if price is None or not size:
        return False
    derived = derive_from_size_and_price(str(size), price)
    if derived is None or derived.unit != "L":
        return False
    product["unit_price"], product["unit"] = derived.amount, "L"
    product["unit_raw"] = f"${derived.amount:.2f}/1L"
    return True


def select_for_shelf(path: str, products: list[dict], log=None, count: int = KEEP_PER_COLOUR) -> tuple[list[dict], int]:
    """Fill in per-litre prices on a wine shelf, and on a white or red shelf keep only the cheapest few.

    Casks are all kept. Shelves that are not wine come back unchanged. Returns (products to keep,
    how many were set aside).
    """
    if not is_wine_shelf(path):
        return products, 0
    filled = sum(fill_unit_price(p) for p in products)
    if log and filled:
        log(f"  wine: price per litre worked out from the size for {filled} wine(s) (the shelf shows none)")
    colour = wine_colour(path)
    if colour is None:
        return products, 0
    kept, notes, report = cheapest_wines(products, count)
    if log:
        for line in notes:
            log(f"  wine: {line}")
        log(f"  wine: cheapest {len(kept)} {colour} of {len(products)} read:")
        for row in report:
            log(f"    {row['name']} - ${row['price']:.2f} - ${row['per_litre']:.2f}/L")
    return kept, len(products) - len(kept)
