"""Throwaway fixture tests. Not product code."""

from title_cleaner import clean_label, sanitize_title


def test_sanitize_title_table():
    cases = [
        ("Hello, world.", "Hello, world."),
        ("a |hop", "a hop"),
        ("tabs\tand   spaces", "tabs and spaces"),
    ]
    for raw, want in cases:
        assert sanitize_title(raw) == want


def test_clean_label_unchanged_text():
    assert clean_label("plain words") == "plain words"
