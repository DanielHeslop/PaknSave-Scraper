"""The one place the app's Authorization header value is built.

Every call to the recipe app (ingredient list, scrape-exclusions, match-terms,
store-categories, price-mailbox, price-runs) sends exactly
"Authorization: Bearer <MAILBOX_TOKEN>". A token that came from an env file can
carry a trailing newline or carriage return, stray spaces or wrapping quotes;
none of those belong in the header, so they are removed here.
"""

from __future__ import annotations


def clean_token(token: str) -> str:
    token = (token or "").strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1].strip()
    return token


def bearer(token: str) -> str:
    return f"Bearer {clean_token(token)}"
