"""Rescale PAK'nSAVE's own displayed unit price to a standard per-kg / per-L figure.

PAK'nSAVE already computes and displays a unit price on each product tile,
e.g. "$0.49/100g" or "$1.64/1L" - this module does not calculate price
divided by size from scratch. It only reads the raw unit text PAK'nSAVE
already shows and rescales it to a standard unit, using Decimal so the
arithmetic behaves like money rather than plain floating point.

"$X/each" (a product sold as a single item, with no weight or volume
comparison) is a normal, valid outcome - not an error. Anything that
doesn't match g/kg/ml/L/each is treated as unreadable (returns None) rather
than guessed at, so a human can look at what shape of text it actually was.
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass

# Matches PAK'nSAVE's own displayed unit price, e.g. "$0.49/100g", "$1.64/1L",
# "$2.00/each". Captures the amount and the raw unit text after the slash.
_UNIT_PRICE_PATTERN = re.compile(r"\$(\d+\.?\d*)\s*/\s*(\w+)")

_GRAMS_PATTERN = re.compile(r"^(\d+)g$")
_ML_PATTERN = re.compile(r"^(\d+)ml$", re.IGNORECASE)


@dataclass
class StandardUnitPrice:
    amount: float
    unit: str  # "kg", "L", or "each"


def _round2(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_raw_unit_price(raw_text: str) -> StandardUnitPrice | None:
    """Find and standardise a "$X/Yunit" style string within raw_text.

    Returns None if no such pattern is found, or if the unit found isn't
    a recognised g/kg/ml/L/each shape.
    """
    match = _UNIT_PRICE_PATTERN.search(raw_text)
    if not match:
        return None

    amount_str, raw_unit = match.group(1), match.group(2)
    try:
        amount = Decimal(amount_str)
    except Exception:
        return None

    unit_lower = raw_unit.lower()

    # "each" - no weight/volume unit at all. Pass the raw price through
    # unchanged. This is the expected, correct outcome for items sold as
    # single units (e.g. the "Belgium Slice - Each" case) - not a bug.
    if unit_lower == "each" or unit_lower == "ea":
        return StandardUnitPrice(amount=_round2(amount), unit="each")

    # Already a standard whole unit ("kg", "1kg", "L", "1l", ...)
    if unit_lower in ("kg", "1kg"):
        return StandardUnitPrice(amount=_round2(amount), unit="kg")
    if unit_lower in ("l", "1l"):
        return StandardUnitPrice(amount=_round2(amount), unit="L")

    # g -> kg: multiply by (1000 / grams_in_the_raw_unit)
    grams_match = _GRAMS_PATTERN.match(unit_lower)
    if grams_match:
        grams = Decimal(grams_match.group(1))
        if grams <= 0:
            return None
        rescaled = amount * (Decimal(1000) / grams)
        return StandardUnitPrice(amount=_round2(rescaled), unit="kg")

    # ml -> L: multiply by (1000 / ml_in_the_raw_unit)
    ml_match = _ML_PATTERN.match(unit_lower)
    if ml_match:
        ml = Decimal(ml_match.group(1))
        if ml <= 0:
            return None
        rescaled = amount * (Decimal(1000) / ml)
        return StandardUnitPrice(amount=_round2(rescaled), unit="L")

    # Unrecognised unit shape - unreadable, not guessed at.
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
