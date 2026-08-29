"""The shape of a single scraped product."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScrapedProduct:
    """One product read off a PAK'nSAVE category page at one point in time.

    A field being None is a normal, honest outcome meaning "this genuinely
    couldn't be read" - it is never a placeholder or a guess. See the
    project README for why this matters (e.g. items sold "each" with no
    per-unit price at all).
    """

    product_id: str
    name: str
    category: str
    size: str | None          # e.g. "500g", "1L", None if never resolved
    price: float | None       # e.g. 5.89, None if unreadable
    unit_price: float | None  # rescaled to standard unit, e.g. 11.78
    unit: str | None          # "kg", "L", or "each" - the unit unit_price is IN
    scraped_at: str           # ISO date, e.g. "2026-08-29"
