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


def _stem(word: str) -> str:
    """Crude plural folding so 'leeks' meets 'leek' and 'tomatoes' meets 'tomato'."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("oes", "ches", "shes", "sses", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def stemmed_words(text: str) -> set[str]:
    return {_stem(w) for w in _significant_words(text)}


def is_local_match(term: str, product_name: str) -> bool:
    """Same shared-word test as is_confident_match, with plurals folded.

    Used where the product list is searched locally (no site search to do
    the stemming for us), so 'courgette' still finds 'Green Courgettes'.
    """
    return bool(stemmed_words(term) & stemmed_words(product_name))
