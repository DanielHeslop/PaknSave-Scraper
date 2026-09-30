"""Publishing this run's results to the recipe app's mailbox (optional, best-effort).

Configured the same way scraper/run.py's PWSCRAPER_CHROMIUM_PATH override is:
plain environment variables, read at call time, no config file.

  MAILBOX_URL   - the mailbox endpoint to POST to.
  MAILBOX_TOKEN - bearer token the mailbox expects.

If either is unset, publishing is silently skipped (one clear log line, not
an error) - this feature is optional and most environments won't have it
configured. See the "Publishing to the recipe app" section in README.md.

Sending is always best-effort: whatever happens here (no internet, a wrong
token, a slow or unreachable mailbox), the scrape itself has already
succeeded and its local files (data/snapshots/, data/price_history.json)
are already written and are the real record. Nothing in this module ever
raises past its own boundary - a failure here only ever prints a plain
warning and returns.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

MAILBOX_URL_ENV = "MAILBOX_URL"
MAILBOX_TOKEN_ENV = "MAILBOX_TOKEN"
TIMEOUT_SECONDS = 5

# Human-readable form of this repo's own "unit" values (see extract.py /
# unit_price.py - "kg", "L", "each" are the only three this repo ever
# produces, identically for Pak'nSave, Woolworths, and New World; verified
# 2026-09-30 against real snapshots for all three chains). Mirrors the
# mapping the receiving app is expected to apply for display (e.g. "(per
# kg)" instead of "(kg)") - kept here too, under one name, so a future
# consumer of this field can use the exact same table rather than
# reinventing it. Anything not covered here (e.g. a raw "100g"-style unit,
# which this repo never actually emits in "unit" but might one day) is
# returned unchanged rather than guessed at.
_UNIT_LABELS = {
    "kg": "per kg",
    "per kg": "per kg",
    "/kg": "per kg",
    "kilo": "per kg",
    "l": "per L",
    "per l": "per L",
    "litre": "per L",
    "liter": "per L",
    "ea": "ea",
    "each": "ea",
    "unit": "ea",
}


def map_unit_label(unit: str | None) -> str | None:
    """Map a raw unit string to its human-readable display form.

    "kg" -> "per kg", "L" -> "per L", "each"/"ea" -> "ea". Matching is
    case-insensitive. None, "", or anything not in the table above (e.g. a
    "per 100g"-style unit) is returned unchanged - never guessed at.
    """
    if not unit:
        return unit
    return _UNIT_LABELS.get(unit.strip().lower(), unit)


def publish_to_mailbox(products: list[dict]) -> None:
    """Best-effort: send this run's priced products to the recipe app's mailbox.

    Any product with a price is sent - both weight/volume items (which have
    a proper per-unit price, e.g. "$2.29/kg") and "each"/pack items with no
    per-unit comparison at all (e.g. a single cucumber, a 6-pack of
    frankfurters). Only products with no price whatsoever are excluded. The
    request body is:

        {"items": [{"name": ..., "price": <per-unit price for kg/L items, e.g. 2.29 for "$2.29/kg"; the shelf price itself for "each"/pack items>, "unit": "kg" | "L" | "each", "unit_label": "per kg" | "per L" | "ea" (map_unit_label() applied to "unit" - a purely additive, human-readable form; "unit" itself is untouched so nothing that already parses "unit" is affected), "size": "500g" / "ea" / "6pk" (omitted if unresolved), "supermarket": "Pak'nSave" | "Woolworths" | "New World", "product_id": the same stable per-chain ID already stored in snapshots/price_history for this item (Pak'nSave/New World: "P#######"; Woolworths: numeric sku as a string)}, ...]}

    "price" is product["unit_price"] when a real per-unit price is
    available (weight/volume items). "each"/pack items have no unit_price
    to rescale from - PAK'nSAVE never displays one for them - so the raw
    shelf price (product["price"]) is sent instead, unchanged: for a
    single-item price that already IS the per-unit price, no invented
    number involved. Their "unit" is forced to "each" for the same reason.
    "size" is always the raw string PAK'nSAVE shows (e.g. "ea", "6pk",
    "500g"), passed through as-is - never parsed or reinterpreted. The list
    is wrapped under "items" because that's the shape the mailbox endpoint
    itself expects - a bare array is not.

    "unit_label" (added 2026-09-30): investigating why New World mailbox
    items were showing "(kg)"/"(L)" suffixes in the app instead of "(per
    kg)"/"(per L)" like the other two chains, real snapshot data confirmed
    this repo's own "unit" field is the bare literal "kg"/"L"/"each" for
    ALL THREE chains identically - Pak'nSave and New World are byte-for-byte
    the same shape for the same products (e.g. loose veg: size="kg",
    unit="kg" for both). So whatever displays Pak'nSave/Woolworths correctly
    today does not do so by reading "unit" verbatim, and the actual
    display-suffix logic lives entirely in the recipe app's own code (not in
    this repo, not available to inspect from here). "unit_label" is added
    as a new, purely additive field - "unit" is left completely unchanged
    for every chain - so the app can adopt it for display without any risk
    to whatever already reads "unit" for calculation. This does NOT, by
    itself, fix the reported display bug; that requires a corresponding
    change in the app to consume "unit_label" (or fix its own per-chain
    branching) - flagged explicitly since it could not be verified here.
    """
    mailbox_url = os.environ.get(MAILBOX_URL_ENV)
    mailbox_token = os.environ.get(MAILBOX_TOKEN_ENV)

    if not mailbox_url or not mailbox_token:
        print(
            "Publishing to the recipe app skipped - MAILBOX_URL/MAILBOX_TOKEN not set."
        )
        return

    payload = [
        {
            "name": p["name"],
            "price": p["unit_price"] if p.get("unit_price") is not None else p["price"],
            "unit": p["unit"] if p.get("unit") is not None else "each",
            "unit_label": map_unit_label(p["unit"] if p.get("unit") is not None else "each"),
            **({"size": p["size"]} if p.get("size") is not None else {}),
            # Defaults to "Pak'nSave" for pre-multi-chain callers/tests that
            # don't set this field - never invented for a record that
            # actually came from elsewhere, since every real product dict is
            # now tagged with its own chain at construction time.
            "supermarket": p.get("supermarket", "Pak'nSave"),
            # Same stable ID already keyed into snapshots/price_history for
            # this item (see storage.py) - lets the receiving app dedupe by
            # (supermarket, product_id) instead of by name, which breaks
            # whenever a name-cleanup fix changes a product's stored name.
            "product_id": p["product_id"],
        }
        for p in products
        if p.get("price") is not None
    ]

    if not payload:
        print("Publishing to the recipe app skipped - no priced products this run.")
        return

    body = json.dumps({"items": payload}).encode("utf-8")
    request = urllib.request.Request(
        mailbox_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {mailbox_token}",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as e:
        print(f"Publishing to the recipe app failed: the mailbox responded with status {e.code} ({e.reason}).")
        return
    except urllib.error.URLError as e:
        print(f"Publishing to the recipe app failed: could not reach {mailbox_url} ({e.reason}).")
        return
    except TimeoutError:
        print(f"Publishing to the recipe app failed: timed out after {TIMEOUT_SECONDS}s.")
        return
    except Exception as e:
        # Genuinely unexpected - still non-fatal for the scrape, but worth
        # naming plainly rather than swallowing silently.
        print(f"Publishing to the recipe app failed: unexpected error ({e!r}).")
        return

    if 200 <= status < 300:
        print(f"Sent {len(payload)} prices to the app.")
    else:
        print(f"Publishing to the recipe app failed: the mailbox responded with status {status}.")
