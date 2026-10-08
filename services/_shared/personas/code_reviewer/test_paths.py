"""Which changed files are tests.

Shared by the lint evidence and the secret scanner, which both raise
high-severity noise on test fixtures (a fake `TOKEN = "..."`). Component match,
not substring, so `contests/x.py` and `latest.py` are not test files.
"""

from __future__ import annotations

_TEST_DIRS = frozenset({"tests", "test", "testing"})


def is_test_file(path: str) -> bool:
    """True for a path COMPONENT named tests/test/testing, or a basename of
    `test_*.py`, `*_test.py` or `conftest.py`."""
    parts = path.split("/")
    if any(p in _TEST_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    return name == "conftest.py" or (
        name.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
    )
