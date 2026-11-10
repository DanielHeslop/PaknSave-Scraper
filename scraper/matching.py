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


_SYNONYMS = {
    "yogurt": "yoghurt",
    "chili": "chilli",
    "chile": "chilli",
    "zucchini": "courgette",
    "cilantro": "coriander",
    "aubergine": "eggplant",
    "arugula": "rocket",
    "mayo": "mayonnaise",
    "cooky": "cookie",
    "molasse": "molass",
    "flavour": "flavoured",
    "flavored": "flavoured",
    "flavor": "flavoured",
}

# A product of one of these forms is not the plain ingredient ("milk" is not
# "milk powder"), so it is rejected unless the term names the same form.
_FORMS = {
    "powder", "mix", "kit", "sauce", "mayonnaise", "granola", "muffin", "cookie",
    "chip", "seasoning", "spread", "flavoured", "drink",
}

# Words that may be dropped, all at once, when nothing matches with them in.
_DESCRIPTORS = {
    "light", "dark", "toasted", "dry", "fresh", "canned", "frozen", "organic",
    "free", "range", "dried", "ground", "chopped", "store", "bought", "homemade",
    "thawed",
}

# Too vague to be evidence of a match on their own ("dry", "white", "light").
_GENERIC = {
    "dry", "white", "light", "fresh", "dark", "red", "green", "black", "brown",
    "hot", "sweet", "cold", "plain", "natural", "whole",
}

_JOINERS = {"&", "and"}
_TOKEN_PATTERN = re.compile(r"[a-z]+|&")

# The main item of a product is its last noun phrase before one of these.
_CONNECTORS = {"with", "in", "infused", "&", "and"}
_MAIN_ITEM_TOKEN = re.compile(r"[a-z0-9]+|&")

# Packaging / pack-shape words (canonical forms) that trail the real item:
# "Garlic Bulb", "Manuka Honey Jar", "Garlic Butter Block".
_PACK_WORDS = {
    "bulb", "jar", "bottle", "tub", "block", "bag", "pack", "tin", "can",
    "punnet", "bunch", "pouch", "sachet", "carton", "tray", "squeeze",
}

# A main noun that is also a kind of another item: tamari is a soy sauce.
_IS_A = {"tamari": {"sauce"}}


def _canon(word: str) -> str:
    stem = _stem(word)
    return _SYNONYMS.get(stem, stem)


def _term_words(term: str) -> list[str]:
    seen: list[str] = []
    for word in _significant_words_ordered(term):
        canon = _canon(word)
        if canon not in seen:
            seen.append(canon)
    return seen


def _significant_words_ordered(text: str) -> list[str]:
    return [w for w in _WORD_PATTERN.findall(text.lower()) if len(w) >= 3 and w not in _STOPWORDS]


class _Term:
    def __init__(self, term: str):
        self.text = term
        self.words = _term_words(term)
        self.forms = {w for w in self.words if w in _FORMS} | {k for w in self.words for k in _IS_A.get(w, ())}
        self.has_joiner = bool(re.search(r"&|\band\b", term.lower()))
        self.has_descriptors = any(w in _DESCRIPTORS for w in self.words)

    def required(self, drop_descriptors: bool) -> list[str]:
        if drop_descriptors:
            return [w for w in self.words if w not in _DESCRIPTORS]
        return self.words


def _product_words(name: str) -> tuple[set[str], set[str]]:
    """Return (canonical words, those not sitting inside a flavour pair).

    A flavour pair is the word either side of '&'/'and' when more words follow,
    as in "Garlic & Herb Butterfly Chicken" - the pair describes the thing that
    follows, it is not the product. "Cream Cheese & Chives" has nothing after
    the pair, so the pair is part of the product itself.
    """
    items: list[str | None] = []  # canonical word, or None for a joiner
    for token in _TOKEN_PATTERN.findall(name.lower()):
        if token in _JOINERS:
            items.append(None)
        elif len(token) >= 3 and token not in _STOPWORDS:
            items.append(_canon(token))
    flavour: set[int] = set()
    for k, item in enumerate(items):
        if item is not None or k == 0 or k + 1 >= len(items):
            continue
        left, right = items[k - 1], items[k + 1]
        if left is None or right is None:
            continue
        if any(x is not None for x in items[k + 2:]):
            flavour.update((k - 1, k + 1))
    everything = {w for w in items if w is not None}
    plain = {w for k, w in enumerate(items) if w is not None and k not in flavour}
    return everything, plain


def _main_item(text: str, cut_at_joiners: bool) -> list[str]:
    """Words of the main item: the last noun phrase before with / & / and / in / infused.

    Brand and size come off naturally: sizes ("250g", "3pk", "(5pk)") are
    dropped, text after a comma or in brackets is a qualifier, and the head
    noun is the last word, so a leading brand never reaches it. Trailing
    descriptors and pack words are trimmed so "Garlic Bulb 3pk" is "garlic".
    "&" / "and" only cut when cut_at_joiners (a term that itself says
    "garlic and herb butter" needs them kept).
    """
    text = re.sub(r"\([^)]*\)", " ", text.lower()).split(",")[0]
    words: list[str] = []
    for token in _MAIN_ITEM_TOKEN.findall(text):
        if token in _CONNECTORS and (cut_at_joiners or token not in _JOINERS):
            if words:
                break
            continue
        if token in _JOINERS or token[0].isdigit() or len(token) < 3 or token in _STOPWORDS:
            continue
        words.append(_canon(token))
    trailing_noise = _DESCRIPTORS | _GENERIC | _PACK_WORDS
    while len(words) > 1 and words[-1] in trailing_noise:
        words.pop()
    return words


def _main_item_matches(term: _Term, name: str) -> bool:
    """The term's main noun must be the product's main item ("honey" is not "honey yoghurt")."""
    cut = not term.has_joiner
    wanted, found = _main_item(term.text, cut), _main_item(name, cut)
    if not wanted or not found:
        return True
    noun = wanted[-1]
    return found[-1] == noun or (noun in found and found[-1] in _IS_A.get(noun, ()))


def _matches(term: _Term, name: str, drop_descriptors: bool) -> bool:
    required = term.required(drop_descriptors)
    if not required or all(w in _GENERIC for w in required):
        return False
    everything, plain = _product_words(name)
    available = everything if term.has_joiner else plain
    if not all(w in available for w in required):
        return False
    if (everything & _FORMS) - term.forms:
        return False
    return _main_item_matches(term, name)


def is_local_match(term: str, product_name: str, *, drop_descriptors: bool = False) -> bool:
    """Strict local match: every main word of the term is in the product name.

    Plurals and the synonyms above are folded. Products of a different form
    (powder, mix, sauce, ...) and products where the term only appears as a
    flavour ("garlic & herb chicken") are rejected, and a term made only of
    generic words ("dry", "white") never matches. The term's main noun must
    also be the product's main item, so "honey" is not "honey yoghurt" and
    "butter" is not "Butter Chicken". Use match_products() to get
    the descriptor-dropping fallback applied across a whole product list.
    """
    return _matches(_Term(term), product_name, drop_descriptors)


def match_products(term: str, products: list[dict]) -> list[dict]:
    """Products matching the term; descriptors are dropped once if none do."""
    parsed = _Term(term)
    matches = [p for p in products if _matches(parsed, p["name"], False)]
    if not matches and parsed.has_descriptors:
        matches = [p for p in products if _matches(parsed, p["name"], True)]
    return matches
