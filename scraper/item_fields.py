"""Turn one scraped product into the price/pack/promo fields the app reads.

Everything here is a pure function of the product dict, so a saved snapshot
can be replayed offline exactly as a live run would send it.

Product dict inputs (all optional except name and price):
  price, unit_price, unit            existing fields (store unit price already parsed)
  unit_raw / promo_unit_raw          the store's own unit-price text, regular / promo
  pack_price / regular_pack_price    price charged now / non-promo price for one pack
  promo_type, promo_label, promo_ends, promo_min_qty
  package_type                       Woolworths packageType ("4 pack")
  sheets_per_item                    sheet count read from a Woolworths description

Older snapshots carry none of the new keys. They still work: the pack price is
`price`, there is no promo, and the unit comes from `unit_price`/`unit`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from scraper.pack_size import PackInfo, parse_pack
from scraper.unit_price import StandardUnitPrice, is_facial_tissue_name, parse_raw_unit_price

ON_PROMO_TYPES = ("sale", "member")
ALL_PROMO_TYPES = ("sale", "member", "multibuy")


def _q(value: float | Decimal, places: int) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _close(a: float, b: float, abs_tol: float, rel_tol: float = 0.03) -> bool:
    diff = abs(a - b)
    return diff <= abs_tol or diff <= rel_tol * max(abs(a), abs(b))


@dataclass
class Computed:
    price: float
    unit: str
    pack_count: int
    sheets_per_item: int | None = None
    total_sheets: int | None = None
    unparsed: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class ItemFields:
    payload: dict
    notes: list[str]


def _compute(
    std: StandardUnitPrice | None,
    raw_text: str | None,
    pack_price: float,
    pack: PackInfo,
    name: str,
) -> Computed:
    count = pack.count
    sheets = pack.sheets_per_item
    notes: list[str] = []
    if pack.count_conflict:
        notes.append("count_conflict")

    if std is None:
        unparsed = raw_text.strip() if raw_text and raw_text.strip() else None
        if count and count > 1:
            return Computed(_q(pack_price / count, 4), "each", count, unparsed=unparsed, notes=notes)
        return Computed(pack_price, "each", 1, unparsed=unparsed, notes=notes)

    if std.unit in ("kg", "L"):
        return Computed(std.amount, std.unit, count or 1, notes=notes)

    if std.unit == "100sheets":
        if (
            sheets is None
            and count
            and count > 1
            and is_facial_tissue_name(name)
            and _close(pack_price / count * 100, std.amount, 0.006)
        ):
            # "200pk" tissues: the store's own figure only fits if 200 is the sheet count.
            notes.append("tissue_pk_is_sheets")
            return Computed(
                _q(pack_price / count * 100, 4), "100sheets", 1,
                sheets_per_item=count, total_sheets=count, notes=notes,
            )
        if sheets and count:
            total = sheets * count
            computed = pack_price / total * 100
            if _close(computed, std.amount, 0.006):
                return Computed(
                    _q(computed, 4), "100sheets", count,
                    sheets_per_item=sheets, total_sheets=total, notes=notes,
                )
            notes.append(f"sheets_mismatch(store={std.amount},computed={_q(computed, 4)})")
            return Computed(std.amount, "100sheets", count, notes=notes)
        if sheets:
            notes.append("sheets_known_count_unknown")
            return Computed(std.amount, "100sheets", 1, sheets_per_item=sheets, notes=notes)
        if not count:
            notes.append("count_unknown")
        return Computed(std.amount, "100sheets", count or 1, notes=notes)

    # std.unit == "each"
    if not count and sheets and sheets > 1 and _close(pack_price, std.amount * sheets, 0.006 * sheets + 0.02):
        # "40 sheets" on a wipes pack: the store's per-each price only fits if each wipe is one of those.
        notes.append("sheets_as_count")
        return Computed(std.amount, "each", sheets, notes=notes)
    if count and count > 1:
        if not _close(pack_price, std.amount * count, 0.006 * count + 0.02):
            notes.append(f"each_mismatch(pack={pack_price},store*count={_q(std.amount * count, 2)})")
        return Computed(std.amount, "each", count, notes=notes)
    if _close(pack_price, std.amount, 0.02):
        return Computed(pack_price, "each", 1, notes=notes)
    # Store says the price is per each and far below the pack price, but no count was found.
    notes.append("each_count_unknown")
    return Computed(pack_price, "each", 1, unparsed=raw_text.strip() if raw_text else None, notes=notes)


def _legacy_std(p: dict) -> StandardUnitPrice | None:
    if p.get("unit_price") is not None and p.get("unit") in ("kg", "L", "each", "100sheets"):
        return StandardUnitPrice(amount=p["unit_price"], unit=p["unit"])
    return None


def _std_for(text: str | None, p: dict, use_legacy: bool) -> StandardUnitPrice | None:
    if text:
        return parse_raw_unit_price(text, p.get("name"))
    return _legacy_std(p) if use_legacy else None


def _scale(c: Computed, factor: float) -> float:
    places = 2 if c.unit in ("kg", "L") else 4
    return _q(c.price * factor, places)


def build_item_fields(p: dict) -> ItemFields:
    name = p.get("name") or ""
    price_now_default = p["price"]
    pack_price = p.get("pack_price")
    if pack_price is None:
        pack_price = price_now_default
    regular_pack_price = p.get("regular_pack_price")
    if regular_pack_price is None:
        regular_pack_price = pack_price

    promo_type = p.get("promo_type") if p.get("promo_type") in ALL_PROMO_TYPES else None
    on_promo = promo_type in ON_PROMO_TYPES and pack_price != regular_pack_price
    if promo_type in ON_PROMO_TYPES and not on_promo:
        promo_type = None

    pack = parse_pack(name, p.get("size"), p.get("package_type"), None)
    if p.get("sheets_per_item") and pack.sheets_per_item is None:
        pack = PackInfo(
            count=pack.count, sheets_per_item=int(p["sheets_per_item"]),
            count_source=pack.count_source, sheets_source="description",
            count_conflict=pack.count_conflict,
        )

    has_raw = "unit_raw" in p or "promo_unit_raw" in p
    regular_text = p.get("unit_raw")
    promo_text = p.get("promo_unit_raw")

    if on_promo:
        if promo_text:
            charged = _compute(_std_for(promo_text, p, False), promo_text, pack_price, pack, name)
            regular = None
            if regular_text:
                regular = _compute(_std_for(regular_text, p, False), regular_text, regular_pack_price, pack, name)
        else:
            # No promo unit text: scale the regular result by the price ratio.
            regular = _compute(
                _std_for(regular_text, p, not has_raw), regular_text, regular_pack_price, pack, name
            )
            charged = Computed(
                _scale(regular, pack_price / regular_pack_price), regular.unit, regular.pack_count,
                regular.sheets_per_item, regular.total_sheets, regular.unparsed, list(regular.notes),
            )
    else:
        text = regular_text
        charged = _compute(_std_for(text, p, not has_raw), text, pack_price, pack, name)
        regular = charged

    notes = list(charged.notes)
    if on_promo:
        if regular is None or regular.unit != charged.unit:
            if regular is not None:
                notes.append("regular_unit_differs")
            regular_price = _scale(charged, regular_pack_price / pack_price)
        else:
            regular_price = regular.price
    else:
        regular_price = charged.price

    payload: dict = {
        "price": charged.price,
        "unit": charged.unit,
        "pack_price": pack_price,
        "pack_count": charged.pack_count,
        "regular_price": regular_price,
        "regular_pack_price": regular_pack_price,
    }
    if promo_type:
        payload["promo_type"] = promo_type
        if p.get("promo_label"):
            payload["promo_label"] = p["promo_label"]
        if p.get("promo_ends"):
            payload["promo_ends"] = p["promo_ends"]
        if promo_type == "multibuy" and p.get("promo_min_qty"):
            payload["promo_min_qty"] = p["promo_min_qty"]
    if charged.sheets_per_item:
        payload["sheets_per_item"] = charged.sheets_per_item
    if charged.total_sheets:
        payload["total_sheets"] = charged.total_sheets
    if charged.unparsed:
        payload["unit_unparsed"] = charged.unparsed

    return ItemFields(payload=payload, notes=notes)

