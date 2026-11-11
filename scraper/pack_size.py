"""Read how many single items a pack holds, and how many sheets each has.

Only explicit text is used: a store subtitle ("8 x 270 sheets", "12pk"),
Woolworths' volumeSize/packageType ("40pack", "4 pack"), the product name
("4pk", "6 x 125g"), or - for sheets - a description line ("180 sheets per
roll"). Nothing is worked out from prices and nothing is guessed. When two
different numbers could both be right, the answer is None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MAX_COUNT = 1000

# "8 x 270 sheets", "6 x 125g", "2 x 60 sheets", "4 x 1L". A unit must follow
# the second number so that dimensions like "30 x 20cm" are not read as packs.
_MULTI = re.compile(
    r"\b(\d{1,3})\s*[x×]\s*(\d+(?:\.\d+)?)\s*(sheets?|kg|g|ml|l|ea|pk|pack|packs)\b",
    re.IGNORECASE,
)
# "12pk", "40pack", "4 pack", "6 pk", "2pkt", "4ea", "pack of 6".
_PACK = re.compile(r"\b(\d{1,4})\s*(?:pk|pks|pack|packs|pkt|pkts|ea)\b", re.IGNORECASE)
# "17s", "30's" in a size field only (in a name, "60's" can mean sheets). Two or three digits.
_COUNT_S = re.compile(r"\b(\d{2,3})'?s\b", re.IGNORECASE)
# Egg sizes and grades ("Size 6 Eggs") are not counts.
_SIZE_GRADE = re.compile(r"\b(?:size|grade)\s+\d+\b", re.IGNORECASE)
_PACK_OF = re.compile(r"\bpack\s+of\s+(\d{1,4})\b", re.IGNORECASE)
# "40 wipes", "12 rolls", "30 tablets": a count of the single item itself.
_ITEMS = re.compile(
    r"\b(\d{1,4})\s*(?:wipes?|rolls?|tablets?|capsules?|pods?|eggs?|nappies|pads?)\b",
    re.IGNORECASE,
)
_SHEETS = re.compile(r"\b(\d{2,4})\s*[- ]?sheets?\b", re.IGNORECASE)


@dataclass(frozen=True)
class PackInfo:
    count: int | None = None
    sheets_per_item: int | None = None
    count_source: str | None = None
    sheets_source: str | None = None
    count_conflict: bool = False


def _count_from(text: str | None, allow_s: bool = False) -> tuple[int | None, int | None]:
    """(count, sheets_per_item) from one string. Either may be None."""
    if not text:
        return None, None
    text = _SIZE_GRADE.sub(" ", text)
    m = _MULTI.search(text)
    if m:
        count = int(m.group(1))
        sheets = None
        if m.group(3).lower().startswith("sheet"):
            value = float(m.group(2))
            if value == int(value) and 2 <= value <= 5000:
                sheets = int(value)
        return (count if 1 <= count <= _MAX_COUNT else None), sheets
    for pattern in (_PACK, _PACK_OF, _ITEMS) + ((_COUNT_S,) if allow_s else ()):
        m = pattern.search(text)
        if m:
            count = int(m.group(1))
            if 1 <= count <= _MAX_COUNT:
                return count, None
    return None, None


def sheets_from_text(text: str | None) -> int | None:
    """A sheets-per-item figure from free text, or None when absent or unclear.

    Only counts when the text names exactly one distinct sheet number. A
    description like "Mega Roll 400 sheets ... our standard 170 sheet roll"
    names two, so it is ambiguous and gives None. "8 x 270 sheets" is a pack
    line, handled by the pack parser, not here.
    """
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    multi = _MULTI.search(text)
    if multi and multi.group(3).lower().startswith("sheet"):
        return None
    found = {int(n) for n in _SHEETS.findall(text)}
    if len(found) == 1:
        return found.pop()
    return None


def parse_pack(
    name: str | None = None,
    size: str | None = None,
    package_type: str | None = None,
    description: str | None = None,
) -> PackInfo:
    """Pack count and sheets per item from whatever explicit text is given.

    Count sources are tried in order: size (subtitle / volumeSize), package
    type, then name. The first one that gives a count wins; if a later source
    gives a different count, count_conflict is set so callers can report it.
    """
    count: int | None = None
    count_source: str | None = None
    sheets: int | None = None
    sheets_source: str | None = None
    conflict = False

    for source, text in (("size", size), ("package_type", package_type), ("name", name)):
        c, s = _count_from(text, allow_s=(source == "size"))
        if c is not None:
            if count is None:
                count, count_source = c, source
            elif c != count:
                conflict = True
        if s is not None and sheets is None:
            sheets, sheets_source = s, source

    if sheets is None:
        for source, text in (("size", size), ("name", name), ("description", description)):
            value = sheets_from_text(text)
            if value is not None:
                sheets, sheets_source = value, source
                break

    return PackInfo(
        count=count,
        sheets_per_item=sheets,
        count_source=count_source,
        sheets_source=sheets_source,
        count_conflict=conflict,
    )
