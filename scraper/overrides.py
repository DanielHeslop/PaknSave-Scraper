"""Read and apply manual corrections from overrides.txt.

Mirrors the reference project's approach: a small hand-maintained text file
keyed by product ID, used to patch the handful of products where automated
scraping gets the size wrong (or can't find one at all), or to drop a
product entirely.

File format, one entry per line:

    P5019270 1020g
    P1234567 skip

Lines starting with # are comments and ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SIZE_PATTERN = re.compile(r"^\d+(\.\d+)?(g|kg|ml|l)$", re.IGNORECASE)


@dataclass
class Override:
    size: str | None = None
    skip: bool = False


def load_overrides(path: str | Path) -> dict[str, Override]:
    """Parse overrides.txt into a dict keyed by product ID."""
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

        product_id, instruction = parts[0], parts[1]

        if instruction.lower() == "skip":
            overrides[product_id] = Override(skip=True)
        elif _SIZE_PATTERN.match(instruction):
            overrides[product_id] = Override(size=instruction)
        # Anything else is an unrecognised instruction shape - ignored
        # rather than guessed at.

    return overrides


def get_override(overrides: dict[str, Override], product_id: str) -> Override | None:
    return overrides.get(product_id)
