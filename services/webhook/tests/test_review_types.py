"""Tests for review_types — the single Severity source (#250)."""
from __future__ import annotations

from typing import get_args

import review_types


def test_severity_vocabulary_is_the_four_levels():
    """Lock the actual vocabulary — an accidental add/drop/rename of a
    level is a calibration change that should fail a test, not ship."""
    assert set(get_args(review_types.Severity)) == {
        "low", "medium", "high", "critical",
    }


def test_severities_frozenset_matches_the_literal():
    """SEVERITIES is derived from Severity (no hand-maintained copy)."""
    assert review_types.SEVERITIES == frozenset(get_args(review_types.Severity))
    assert isinstance(review_types.SEVERITIES, frozenset)
