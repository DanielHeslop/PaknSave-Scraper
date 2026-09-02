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
    """Best-effort: send this run's complete products to the recipe app's mailbox.

    Only products with BOTH a price and a unit price are sent (the mailbox
    isn't built to handle partial entries - e.g. "each" items with no unit
    price are left out here, same as they're left out of any per-kg
    comparison). Each entry sent is:

        {"name": ..., "price": <per-unit price, e.g. 2.29 for "$2.29/kg">, "unit": "kg" | "L" | "each"}

    Note "price" in the outgoing payload is deliberately the per-unit price
    (product["unit_price"]), not the raw shelf price - that's what the
    mailbox's existing price-book already expects.
    """
    mailbox_url = os.environ.get(MAILBOX_URL_ENV)
    mailbox_token = os.environ.get(MAILBOX_TOKEN_ENV)

    if not mailbox_url or not mailbox_token:
        print(
            "Publishing to the recipe app skipped - MAILBOX_URL/MAILBOX_TOKEN not set."
        )
        return

    payload = [
        {"name": p["name"], "price": p["unit_price"], "unit": p["unit"]}
        for p in products
        if p.get("price") is not None and p.get("unit_price") is not None
    ]

    if not payload:
        print("Publishing to the recipe app skipped - no complete products (price + unit price) this run.")
        return

    body = json.dumps(payload).encode("utf-8")
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
