"""Tests for scraper/woolworths_transport.py's department-label prefix
stripping (see _strip_department_prefix's docstring for why this exists).
"""

from __future__ import annotations

from scraper.woolworths_transport import _strip_department_prefix


def test_strips_fresh_vegetable_prefix():
    assert _strip_department_prefix("fresh vegetable red onion (ea)") == "red onion (ea)"


def test_strips_fresh_fruit_prefix():
    assert _strip_department_prefix("fresh fruit bananas yellow loose") == "bananas yellow loose"


def test_strips_fresh_vegetables_plural_prefix():
    assert _strip_department_prefix("fresh vegetables broccoli") == "broccoli"


def test_strips_fresh_fruits_plural_prefix():
    assert _strip_department_prefix("fresh fruits kiwifruit") == "kiwifruit"


def test_is_case_insensitive_but_preserves_remainder_casing():
    assert _strip_department_prefix("Fresh Vegetable Red Onion") == "Red Onion"


def test_no_matching_prefix_is_a_no_op():
    assert _strip_department_prefix("woolworths nz beef mince grass fed 5% fat") == (
        "woolworths nz beef mince grass fed 5% fat"
    )


def test_prefix_only_stripped_when_leading_not_embedded():
    # Seen live: the phrase appears mid-name, not as a genuine leading
    # department label - must be left untouched.
    name = "woolworths fresh vegetable beans green"
    assert _strip_department_prefix(name) == name

    name = "the odd bunch fresh vegetable carrots"
    assert _strip_department_prefix(name) == name


def test_similar_but_distinct_leading_text_is_not_stripped():
    # Real brand name seen live ("Fresh 'n Fruity" yoghurt) - "fresh n" is
    # not "fresh fruit ", so this must not be mistaken for the noise prefix.
    name = "fresh n fruity yoghurt greek strawberry"
    assert _strip_department_prefix(name) == name
