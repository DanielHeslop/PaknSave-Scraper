"""Fetch the "wanted" ingredient list from the recipe app.

The recipe app (the same one scraper/publish.py sends prices to) exposes a
read-only endpoint listing every ingredient name used across its recipes.
This module only ever GETs that list - it never writes anything back to the
recipe app itself (that's publish.py's job, and only for matched results).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_INGREDIENT_LIST_URL = "https://my-recipe-manager.netlify.app/api/ingredient-list"
TIMEOUT_SECONDS = 15

# Same env var scraper/publish.py reads its Bearer token from - the app's
# ingredient-list, price-mailbox, and scrape-exclusions endpoints all accept
# this one scraper token, so there's only ever one credential to configure.
MAILBOX_TOKEN_ENV = "MAILBOX_TOKEN"


class IngredientListError(Exception):
    """Raised when the ingredient list can't be fetched or parsed at all.

    Unlike publish.py's best-effort failures, this one is fatal to a search
    run - without a list of ingredients there is nothing to search for.
    """


def fetch_ingredient_list(url: str = DEFAULT_INGREDIENT_LIST_URL) -> list[str]:
    """GET the ingredient list and return it as a plain list of names.

    Sends "Authorization: Bearer <MAILBOX_TOKEN>" when that env var is set -
    same env var and header scraper/publish.py uses for price-mailbox and
    scrape-exclusions, since the app expects the same scraper token on all
    three endpoints. If MAILBOX_TOKEN is unset, the request is sent without
    an Authorization header, same as before (the app may still allow this
    endpoint to be read anonymously; if not, the resulting 401 surfaces via
    IngredientListError same as any other failure).

    Raises IngredientListError with a clear reason on any failure (network,
    HTTP status, or unexpected JSON shape) rather than returning a partial
    or guessed-at list. Never logs the token itself.
    """
    headers = {"Accept": "application/json"}
    mailbox_token = os.environ.get(MAILBOX_TOKEN_ENV)
    if mailbox_token:
        headers["Authorization"] = f"Bearer {mailbox_token}"
    request = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as e:
        raise IngredientListError(f"{url} responded with status {e.code} ({e.reason})") from e
    except urllib.error.URLError as e:
        raise IngredientListError(f"could not reach {url} ({e.reason})") from e
    except TimeoutError as e:
        raise IngredientListError(f"{url} timed out after {TIMEOUT_SECONDS}s") from e

    if not (200 <= status < 300):
        raise IngredientListError(f"{url} responded with status {status}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise IngredientListError(f"{url} did not return valid JSON: {e}") from e

    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise IngredientListError(f"{url} did not return a JSON array of strings")

    return data
