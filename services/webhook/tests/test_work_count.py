"""The line count the linearity tests rely on sees a quadratic loop that makes
no Python call, which a call count does not (grug#1715)."""

from __future__ import annotations

import pytest

from tests._work_count import assert_linear, counted_lines


def _inline_quadratic(items):
    hits = 0
    for a in items:
        for b in items:
            if a == b:
                hits += 1
    return hits


def _linear(items):
    seen = set()
    for a in items:
        seen.add(a)
    return len(seen)


def test_the_line_count_grows_about_four_times_for_an_inline_quadratic_loop():
    _, small = counted_lines(lambda: _inline_quadratic(list(range(100))))
    _, large = counted_lines(lambda: _inline_quadratic(list(range(200))))
    assert large > 3.5 * small, (small, large)


def test_assert_linear_refuses_an_inline_quadratic_loop():
    with pytest.raises(AssertionError):
        assert_linear(lambda n: counted_lines(lambda: _inline_quadratic(list(range(n))))[1], 100)


def test_assert_linear_passes_a_linear_loop():
    assert_linear(lambda n: counted_lines(lambda: _linear(list(range(n))))[1], 1000)
