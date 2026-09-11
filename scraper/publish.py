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


def publish_to_mailbox(products: list[dict]) -> None:
    """Best-effort: send this run's priced products to the recipe app's mailbox.

    Any product with a price is sent - both weight/volume items (which have
    a proper per-unit price, e.g. "$2.29/kg") and "each"/pack items with no
    per-unit comparison at all (e.g. a single cucumber, a 6-pack of
    frankfurters). Only products with no price whatsoever are excluded. The
    request body is:

        {"items": [{"name": ..., "price": <per-unit price for kg/L items, e.g. 2.29 for "$2.29/kg"; the shelf price itself for "each"/pack items>, "unit": "kg" | "L" | "each", "size": "500g" / "ea" / "6pk" (omitted if unresolved)}, ...]}

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
            **({"size": p["size"]} if p.get("size") is not None else {}),
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
