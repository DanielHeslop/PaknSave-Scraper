"""Tests for scraper/overrides.py's chain-aware compound keying - new test
module because this data format had no coverage before, and it's
correctness-critical (a bug here means an override meant for one chain
could silently apply to another's identically-numbered product).

All tests operate on tmp_path files only - never the real overrides.txt.
"""

from __future__ import annotations

from scraper.overrides import chain_key, get_override, load_overrides


def test_unprefixed_line_applies_to_paknsave_only(tmp_path):
    path = tmp_path / "overrides.txt"
    path.write_text("P5040098 skip\n")

    overrides = load_overrides(path)

    assert get_override(overrides, "Pak'nSave", "P5040098").skip is True
    assert get_override(overrides, "New World", "P5040098") is None


def test_prefixed_line_applies_only_to_that_chain(tmp_path):
    path = tmp_path / "overrides.txt"
    path.write_text("newworld:P5040098 skip\n")

    overrides = load_overrides(path)

    assert get_override(overrides, "New World", "P5040098").skip is True
    assert get_override(overrides, "Pak'nSave", "P5040098") is None


def test_same_product_id_different_chains_do_not_collide(tmp_path):
    path = tmp_path / "overrides.txt"
    path.write_text("P5040098 skip\nnewworld:P5040098 1020g\n")

    overrides = load_overrides(path)

    assert get_override(overrides, "Pak'nSave", "P5040098").skip is True
    assert get_override(overrides, "New World", "P5040098").size == "1020g"


def test_size_override_still_works_unprefixed(tmp_path):
    path = tmp_path / "overrides.txt"
    path.write_text("P5019270 1020g\n")

    overrides = load_overrides(path)

    override = get_override(overrides, "Pak'nSave", "P5019270")
    assert override.size == "1020g"
    assert override.skip is False


def test_comments_and_blank_lines_ignored(tmp_path):
    path = tmp_path / "overrides.txt"
    path.write_text("# a comment\n\nP5040098 skip\n")

    overrides = load_overrides(path)

    assert len(overrides) == 1


def test_chain_key_normalizes_supermarket_names():
    assert chain_key("Pak'nSave") == "paknsave"
    assert chain_key("New World") == "newworld"
    assert chain_key("Woolworths") == "woolworths"


def test_missing_file_returns_empty(tmp_path):
    assert load_overrides(tmp_path / "does-not-exist.txt") == {}
