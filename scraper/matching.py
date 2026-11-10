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

# The main item of a product is its last food noun. "with" / "in" end the item
# only when a food noun comes before them.
_CUT_WORDS = {"with", "in"}
_MAIN_ITEM_TOKEN = re.compile(r"[a-z0-9]+|&")

# Packaging / pack-shape words (canonical forms) that trail the real item:
# "Garlic Bulb", "Manuka Honey Jar", "Garlic Butter Block".
_PACK_WORDS = {
    "bulb", "jar", "bottle", "tub", "block", "bag", "pack", "tin", "can",
    "punnet", "bunch", "pouch", "sachet", "carton", "tray", "squeeze",
}

# Trailing words that are not food nouns ("Greek Style", "Tomatoes Chopped").
_NON_NOUNS = {"style", "greek"}

# Words that name a kind of food without saying which ("Focaccia Bread"). When
# one is the main item, the term still matches if its words come right before.
_GENERIC_ITEMS = {"cereal", "bread", "loaf", "sauce", "roll", "spread", "paste", "mix"}


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
        self.forms = {w for w in self.words if w in _FORMS}
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


def _is_noise(word: str) -> bool:
    participle = len(word) >= 5 and word.endswith("ed") and not word.endswith(("eed", "ead"))
    return participle or word in _DESCRIPTORS or word in _GENERIC or word in _PACK_WORDS or word in _NON_NOUNS


def _trim_trailing(words: list[str]) -> list[str]:
    words = list(words)
    while len(words) > 1 and (words[-1] == "&" or _is_noise(words[-1])):
        words.pop()
    return words


def _main_item(text: str, lone_food: bool | str | None) -> tuple[list[str], set[str]]:
    """The main item of a name: (its words, the words that can be its head noun).

    Size, bracketed text and anything after a comma are dropped, and so are
    trailing pack words, descriptors and participles ("Garlic Bulb 3pk" is
    garlic). The head is the last word, so a leading brand never counts.
    "&" and "and" never end the item. "with" / "in" end it only when a food
    noun comes before them; a lone word there could be a brand ("Yoplait With
    Real Blueberry Yoghurt"), so it counts only if it is lone_food (True for a
    term, the term's noun for a product). A final single-word pair ("Cream
    Cheese & Chives") has two heads.
    """
    text = re.sub(r"\([^)]*\)", " ", text.lower()).split(",")[0]
    tokens: list[str] = []
    for token in _MAIN_ITEM_TOKEN.findall(text):
        if token in _CUT_WORDS or token == "&":
            tokens.append(token)
        elif not (token[0].isdigit() or len(token) < 3 or token in _STOPWORDS):
            tokens.append(_canon(token))
    words = tokens
    for index, token in enumerate(tokens):
        if token not in _CUT_WORDS:
            continue
        before = _trim_trailing([w for w in tokens[:index] if w not in _CUT_WORDS and w != "&"])
        if before and (len(before) > 1 or lone_food is True or before[0] == lone_food):
            words = tokens[:index]
            break
    words = _trim_trailing([w for w in words if w not in _CUT_WORDS])
    phrase = [w for w in words if w != "&"]
    heads = {phrase[-1]} if phrase else set()
    if "&" in words:
        last = len(words) - 1 - words[::-1].index("&")
        if len(words) - last == 2 and last >= 1 and words[last - 1] != "&":
            heads.add(words[last - 1])
    return phrase, heads


def _main_item_match(term: _Term, name: str) -> tuple[bool, set[str]]:
    """(does the term's main noun name the product's main item, form words this excuses).

    A generic main item ("Focaccia Bread", "Tamari Soy Sauce", "Weet-Bix
    Breakfast Cereal") also matches a term whose words come right before it,
    with at most one kind word in between. For a form word like "sauce" one is
    required, so "tomato" still doesn't match "Tomato Sauce".
    """
    wanted, _ = _main_item(term.text, True)
    found, heads = _main_item(name, wanted[-1] if wanted else None)
    if not wanted or not found or wanted[-1] in heads:
        return True, set()
    head = found[-1]
    if head not in _GENERIC_ITEMS or (head == "mix" and "mix" not in term.words):
        return False, set()
    before = found[:-1]
    for gap in (0, 1):
        end = len(before) - gap
        if end >= len(wanted) and before[end - len(wanted):end] == wanted:
            if head in _FORMS and gap == 0:
                return False, set()
            return True, {head}
    return False, set()


def _matches(term: _Term, name: str, drop_descriptors: bool) -> bool:
    required = term.required(drop_descriptors)
    if not required or all(w in _GENERIC for w in required):
        return False
    everything, plain = _product_words(name)
    available = everything if term.has_joiner else plain
    if not all(w in available for w in required):
        return False
    matched, excused = _main_item_match(term, name)
    return matched and not ((everything & _FORMS) - term.forms - excused)


def is_local_match(term: str, product_name: str, *, drop_descriptors: bool = False) -> bool:
    """Strict local match: every main word of the term is in the product name.

    Plurals and the synonyms above are folded. Products of a different form
    (powder, mix, sauce, ...) and products where the term only appears as a
    flavour ("garlic & herb chicken") are rejected, and a term made only of
    generic words ("dry", "white") never matches. The term's main noun must
    also be the product's main item (its last food noun), so "honey" is not
    "honey yoghurt" and "butter" is not "Butter Chicken". Use match_products() to get
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
