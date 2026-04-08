"""Tests for the setup wizard data structures and helpers.

Catches integration bugs like:
- LC_METHODS missing from stan.setup
- generate_pseudonym format issues
- is_pseudonym failing to identify generated names
"""

from __future__ import annotations


# ── 1. LC_METHODS structure ──────────────────────────────────────


def test_lc_methods_is_list() -> None:
    """LC_METHODS must be a list, not a dict or other type."""
    from stan.setup import LC_METHODS

    assert isinstance(LC_METHODS, list)


def test_lc_methods_required_keys() -> None:
    """Every LC method entry must have name, spd, and gradient_min."""
    from stan.setup import LC_METHODS

    for i, method in enumerate(LC_METHODS):
        assert isinstance(method, dict), f"LC_METHODS[{i}] is not a dict"
        assert "name" in method, f"LC_METHODS[{i}] missing 'name'"
        assert "spd" in method, f"LC_METHODS[{i}] missing 'spd'"
        assert "gradient_min" in method, f"LC_METHODS[{i}] missing 'gradient_min'"


def test_lc_methods_spd_values_are_ints() -> None:
    """SPD values must be integers for downstream math."""
    from stan.setup import LC_METHODS

    for method in LC_METHODS:
        assert isinstance(method["spd"], int), (
            f"'{method['name']}' has spd={method['spd']!r} which is not int"
        )


def test_lc_methods_gradient_min_types() -> None:
    """gradient_min must be int/float for real methods, or None for custom."""
    from stan.setup import LC_METHODS

    for method in LC_METHODS:
        if method["spd"] > 0:
            assert isinstance(method["gradient_min"], (int, float)), (
                f"'{method['name']}' has gradient_min={method['gradient_min']!r} "
                f"but spd > 0 — should be numeric"
            )
        # spd == 0 entries can have gradient_min = None (custom)


def test_lc_methods_names_are_strings() -> None:
    """Method names should be non-empty strings."""
    from stan.setup import LC_METHODS

    for method in LC_METHODS:
        assert isinstance(method["name"], str)
        assert len(method["name"]) > 0


# ── 2. generate_pseudonym ────────────────────────────────────────


def test_generate_pseudonym_format() -> None:
    """generate_pseudonym should return 'Adjective Scientist' format."""
    from stan.community.pseudonym import generate_pseudonym

    name = generate_pseudonym()
    parts = name.split()
    assert len(parts) == 2, f"Expected 'Adjective Scientist', got {name!r}"
    assert parts[0][0].isupper(), f"Adjective should be capitalized: {name!r}"
    assert parts[1][0].isupper(), f"Scientist should be capitalized: {name!r}"


def test_generate_pseudonym_deterministic_with_seed() -> None:
    """Same seed should produce the same pseudonym."""
    from stan.community.pseudonym import generate_pseudonym

    name1 = generate_pseudonym(seed="test-lab-123")
    name2 = generate_pseudonym(seed="test-lab-123")
    assert name1 == name2

    # Different seed should (very likely) produce a different name
    name3 = generate_pseudonym(seed="other-lab-456")
    # Not guaranteed different, but with 2500 combos it almost certainly is
    # We don't assert inequality to avoid flaky tests


def test_generate_pseudonym_uses_word_lists() -> None:
    """Generated names should use words from the defined word lists."""
    from stan.community.pseudonym import ADJECTIVES, SCIENTISTS, generate_pseudonym

    for _ in range(20):
        name = generate_pseudonym()
        adj, sci = name.split()
        assert adj in ADJECTIVES, f"Adjective {adj!r} not in ADJECTIVES list"
        assert sci in SCIENTISTS, f"Scientist {sci!r} not in SCIENTISTS list"


# ── 3. is_pseudonym ──────────────────────────────────────────────


def test_is_pseudonym_identifies_generated_names() -> None:
    """is_pseudonym should return True for names from generate_pseudonym."""
    from stan.community.pseudonym import generate_pseudonym, is_pseudonym

    for _ in range(10):
        name = generate_pseudonym()
        assert is_pseudonym(name), f"is_pseudonym should recognize {name!r}"


def test_is_pseudonym_rejects_real_names() -> None:
    """is_pseudonym should return False for names not in the word lists."""
    from stan.community.pseudonym import is_pseudonym

    assert is_pseudonym("Brett Phinney") is False
    assert is_pseudonym("Anonymous Lab") is False
    assert is_pseudonym("UC Davis Proteomics") is False


def test_is_pseudonym_rejects_single_words() -> None:
    """is_pseudonym requires exactly two words."""
    from stan.community.pseudonym import is_pseudonym

    assert is_pseudonym("Nimble") is False
    assert is_pseudonym("") is False
    assert is_pseudonym("Nimble Edman Extra") is False


def test_is_pseudonym_case_sensitive() -> None:
    """is_pseudonym should be case-sensitive (word lists are title case)."""
    from stan.community.pseudonym import is_pseudonym

    # These are valid words but wrong case
    assert is_pseudonym("nimble edman") is False
    assert is_pseudonym("NIMBLE EDMAN") is False


# ── 4. Word list sanity ──────────────────────────────────────────


def test_word_lists_not_empty() -> None:
    """Both word lists must have entries for pseudonym generation to work."""
    from stan.community.pseudonym import ADJECTIVES, SCIENTISTS

    assert len(ADJECTIVES) >= 10, "ADJECTIVES list is suspiciously short"
    assert len(SCIENTISTS) >= 10, "SCIENTISTS list is suspiciously short"


def test_word_lists_no_duplicates() -> None:
    """Word lists should not contain duplicates."""
    from stan.community.pseudonym import ADJECTIVES, SCIENTISTS

    assert len(ADJECTIVES) == len(set(ADJECTIVES)), "ADJECTIVES has duplicates"
    assert len(SCIENTISTS) == len(set(SCIENTISTS)), "SCIENTISTS has duplicates"


def test_word_lists_title_case() -> None:
    """All words should be title case for consistent display."""
    from stan.community.pseudonym import ADJECTIVES, SCIENTISTS

    for word in ADJECTIVES:
        assert word[0].isupper(), f"ADJECTIVE {word!r} is not title case"
    for word in SCIENTISTS:
        assert word[0].isupper(), f"SCIENTIST {word!r} is not title case"
