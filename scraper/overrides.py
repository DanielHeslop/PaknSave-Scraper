"""Read and apply manual corrections from overrides.txt.

Mirrors the reference project's approach: a small hand-maintained text file
keyed by product ID, used to patch the handful of products where automated
scraping gets the size wrong (or can't find one at all), or to drop a
product entirely.

File format, one entry per line:

    P5019270 1020g
    P1234567 skip

An unprefixed product ID applies to Pak'nSave, unchanged from every line
this file has ever had - it's the only chain overrides.txt has applied to
until now. To target a different chain (product IDs are NOT globally
unique across chains - see scraper/storage.py's module docstring),
prefix the ID with that chain's lowercase, punctuation-free name and a
colon, e.g.:

    newworld:P5019270 1020g
    newworld:P1234567 skip

The prefix is derived the same way from any chain's "supermarket" string
(scraper.overrides.chain_key), so this isn't New-World-specific - it works
for "Woolworths" or any future chain's products the same way, the day
overrides.txt gains a line for one.

Lines starting with # are comments and ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SIZE_PATTERN = re.compile(r"^\d+(\.\d+)?(g|kg|ml|l)$", re.IGNORECASE)

# Unprefixed lines mean this chain - matches every line overrides.txt has
# ever had, since Pak'nSave is the only chain that has used this file so
# far (mirrors LEGACY_DEFAULT_SUPERMARKET's same assumption in storage.py).
DEFAULT_CHAIN_KEY = "paknsave"


@dataclass
class Override:
    size: str | None = None
    skip: bool = False


def chain_key(supermarket: str) -> str:
    """Turn a supermarket display name ("Pak'nSave", "New World") into the
    lowercase, punctuation-free key used as an overrides.txt line prefix
    ("paknsave", "newworld") - generic string normalization, not a
    hardcoded per-chain table, so a new chain needs no changes here.
    """
    return re.sub(r"[^a-z0-9]", "", supermarket.lower())


def load_overrides(path: str | Path) -> dict[str, Override]:
    """Parse overrides.txt into a dict keyed by "<chain_key>:<product_id>"."""
    overrides: dict[str, Override] = {}
    path = Path(path)
    if not path.exists():
        return overrides

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        raw_id, instruction = parts[0], parts[1]
        if ":" in raw_id:
            chain, product_id = raw_id.split(":", 1)
            chain = chain.lower()
        else:
            chain, product_id = DEFAULT_CHAIN_KEY, raw_id
        key = f"{chain}:{product_id}"

        if instruction.lower() == "skip":
            overrides[key] = Override(skip=True)
        elif _SIZE_PATTERN.match(instruction):
            overrides[key] = Override(size=instruction)
        # Anything else is an unrecognised instruction shape - ignored
        # rather than guessed at.

    return overrides


def get_override(overrides: dict[str, Override], supermarket: str, product_id: str) -> Override | None:
    return overrides.get(f"{chain_key(supermarket)}:{product_id}")
