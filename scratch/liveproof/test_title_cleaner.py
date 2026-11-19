"""Throwaway fixture tests. Not product code."""

import pytest

from title_cleaner import clean_label, sanitize_title

CASES = [
    ("Hello, world.", "Hello, world."),
    ("a |hop", "a hop"),
    ("tabs\tand   spaces", "tabs and spaces"),
]


@pytest.mark.parametrize(("raw", "want"), CASES)
def test_sanitize_title(raw, want):
    assert sanitize_title(raw) == want


def test_clean_label_unchanged_text():
    assert clean_label("plain words") == "plain words"
