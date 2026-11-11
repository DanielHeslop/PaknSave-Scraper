"""Rescale PAK'nSAVE's own displayed unit price to a standard per-kg / per-L figure.

PAK'nSAVE already computes and displays a unit price on each product tile,
e.g. "$0.49/100g" or "$1.64/1L" - this module does not calculate price
divided by size from scratch. It only reads the raw unit text PAK'nSAVE
already shows and rescales it to a standard unit, using Decimal so the
arithmetic behaves like money rather than plain floating point.

"$X/each" (a product sold as a single item, with no weight or volume
comparison) is a normal, valid outcome - not an error. Anything that
doesn't match g/kg/ml/L/each/sheets is treated as unreadable (returns None)
rather than guessed at, so a human can look at what shape of text it actually was.
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass

# Matches a store's displayed unit price, e.g. "$0.49/100g", "$1.64/1L",
# "$2.00/each", "$0.26/100 sheets", "$0.39/100ss", "$1.05/100". Captures the
# amount, the quantity after the slash (optional) and the unit word (optional).
# A space between the quantity and the word is allowed.
_UNIT_PRICE_PATTERN = re.compile(
    r"\$(\d+\.?\d*)\s*(?:/|\bper\b)\s*(\d+\.?\d*)?\s*([A-Za-z]+)?",
    re.IGNORECASE,
)

_KG_WORDS = {"kg", "kilo", "kilos", "kilogram", "kilograms"}
_G_WORDS = {"g", "gm", "gram", "grams"}
_L_WORDS = {"l", "litre", "litres", "liter", "liters"}
_ML_WORDS = {"ml"}
_EACH_WORDS = {"each", "ea", "item", "items"}
_WIPE_NAME = re.compile(r"\bwipes?\b", re.IGNORECASE)
_SHEET_WORDS = {"sheet", "sheets", "ss", "sht", "shts"}

# Facial-tissue products: a bare "$1.05/100" there means per 100 sheets.
# Toilet paper, paper towels and wipes are deliberately not matched.
_TISSUE_PATTERN = re.compile(r"\b(tissues?|facials?)\b", re.IGNORECASE)
_NOT_TISSUE_PATTERN = re.compile(r"\b(toilet|towels?|wipes?|bathroom)\b", re.IGNORECASE)


@dataclass
class StandardUnitPrice:
    amount: float
    unit: str  # "kg", "L", "each" or "100sheets"


def _round2(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _round4(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def is_facial_tissue_name(name: str | None) -> bool:
    if not name:
        return False
    return bool(_TISSUE_PATTERN.search(name)) and not _NOT_TISSUE_PATTERN.search(name)


def parse_raw_unit_price(raw_text: str, name: str | None = None) -> StandardUnitPrice | None:
    """Find and standardise a "$X/Yunit" style string within raw_text.

    Returns None if no such pattern is found, or if the unit isn't one of
    kg, L, each or 100sheets after rescaling. `name` is only used for the
    bare "$1.05/100" shape, which counts as per 100 sheets for facial
    tissues and is left unparsed for everything else.
    """
    if not raw_text:
        return None
    match = _UNIT_PRICE_PATTERN.search(raw_text)
    if not match:
        return None

    amount_str, qty_str, word = match.group(1), match.group(2), match.group(3)
    try:
        amount = Decimal(amount_str)
        qty = Decimal(qty_str) if qty_str else None
    except Exception:
        return None
    if qty is not None and qty <= 0:
        return None

    word_lower = word.lower() if word else None
    per = qty if qty is not None else Decimal(1)

    if word_lower is None:
        # "$1.05/100" - only facial tissues, and only with an explicit number.
        if qty is not None and is_facial_tissue_name(name):
            return StandardUnitPrice(amount=_round4(amount * Decimal(100) / qty), unit="100sheets")
        return None

    if word_lower in _EACH_WORDS:
        if per == 1:
            return StandardUnitPrice(amount=_round2(amount), unit="each")
        return StandardUnitPrice(amount=_round4(amount / per), unit="each")
    if word_lower in _KG_WORDS:
        return StandardUnitPrice(amount=_round2(amount / per), unit="kg")
    if word_lower in _L_WORDS:
        return StandardUnitPrice(amount=_round2(amount / per), unit="L")
    if word_lower in _G_WORDS:
        return StandardUnitPrice(amount=_round2(amount * Decimal(1000) / per), unit="kg")
    if word_lower in _ML_WORDS:
        return StandardUnitPrice(amount=_round2(amount * Decimal(1000) / per), unit="L")
    if word_lower in _SHEET_WORDS:
        if name and _WIPE_NAME.search(name):
            # Wipes are counted in "sheets" by Woolworths; one sheet is one wipe.
            return StandardUnitPrice(amount=_round4(amount / per), unit="each")
        return StandardUnitPrice(amount=_round4(amount * Decimal(100) / per), unit="100sheets")

    # per wash, per metre, per 100 (capsules) and so on: not one of our four units.
    return None


_SIZE_PATTERN = re.compile(r"^(\d+\.?\d*)\s*(g|kg|ml|l)$", re.IGNORECASE)


def derive_from_size_and_price(size: str, price: float) -> StandardUnitPrice | None:
    """Calculate a standard unit price from price / size.

    This is NOT the primary way unit price is obtained (see module
    docstring - PAK'nSAVE's own displayed value is used for that). It
    exists only for the one specific case where an override in
    overrides.txt supplies a corrected size and the unit price needs
    recalculating to match. Returns None if the size doesn't match a
    recognised g/kg/ml/L shape, rather than guessing.
    """
    match = _SIZE_PATTERN.match(size.strip())
    if not match:
        return None

    quantity_str, unit = match.group(1), match.group(2).lower()
    try:
        quantity = Decimal(quantity_str)
    except Exception:
        return None
    if quantity <= 0:
        return None

    price_decimal = Decimal(str(price))

    if unit == "g":
        rescaled = price_decimal * (Decimal(1000) / quantity)
        return StandardUnitPrice(amount=_round2(rescaled), unit="kg")
    if unit == "kg":
        rescaled = price_decimal / quantity
        return StandardUnitPrice(amount=_round2(rescaled), unit="kg")
    if unit == "ml":
        rescaled = price_decimal * (Decimal(1000) / quantity)
        return StandardUnitPrice(amount=_round2(rescaled), unit="L")
    if unit == "l":
        rescaled = price_decimal / quantity
        return StandardUnitPrice(amount=_round2(rescaled), unit="L")

    return None
