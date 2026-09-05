"""Honestly decide whether a search result actually matches what was searched for.

Deliberately simple, on purpose: this is not a fuzzy/similarity matcher. It
only checks whether a meaningful word from the searched ingredient name
turns up in the candidate product's name (case-insensitive) - the same
"does the searched word appear in the product name" test that works for a
plain single-word search like "milk", generalised to multi-word ingredient
phrases by checking each of their words individually.

If nothing shared turns up, that's an honest "no confident match" - not a
bug to work around by loosening this further.
"""

from __future__ import annotations

import re

# Purely grammatical filler words - not food descriptors - excluded so a
# match can't be claimed on "of", "to", "for" etc. alone. Deliberately NOT a
# list of cooking terms like "chopped" or "fresh": trimming those would be
# guessing at which words "count", which is exactly what this module avoids.
_STOPWORDS = {
    "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "with",
    "from", "into", "about", "if", "only", "each", "plus", "the", "your",
    "as", "than", "such", "e.g", "eg",
}

_WORD_PATTERN = re.compile(r"[a-z]+")


def _significant_words(text: str) -> set[str]:
    words = _WORD_PATTERN.findall(text.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def matching_words(ingredient_name: str, product_name: str) -> set[str]:
    """Words shared between the ingredient name and the product name."""
    return _significant_words(ingredient_name) & _significant_words(product_name)


def is_confident_match(ingredient_name: str, product_name: str) -> bool:
    """True if the ingredient and product name share at least one meaningful word.

    An ingredient name with no significant words at all (e.g. all filler)
    can never confidently match anything.
    """
    return bool(matching_words(ingredient_name, product_name))
