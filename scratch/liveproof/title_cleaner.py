"""Throwaway fixture for observing the reviewer. Not product code."""

import re

_ALLOWED = re.compile(r"[^A-Za-z0-9 ,.]")


def sanitize_title(text: str) -> str:
    """Keep letters, digits, space, comma and dot; collapse the rest."""
    kept = _ALLOWED.sub(" ", text)
    return re.sub(r"\s+", " ", kept).strip()


def clean_label(text: str) -> str:
    """Collapse runs of whitespace into one space."""
    return re.sub(r"\s+", " ", text).strip()
