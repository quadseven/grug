"""Work counts for linearity tests (grug#1715).

A linearity test that times a scan flakes on a loaded host. Counting Python
function calls instead misses a quadratic loop whose inner body calls
nothing. Counting executed lines sees both: every iteration of a Python
loop, nested or not, runs at least one line.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any


def counted_lines(fn: Callable[[], Any]) -> tuple[Any, int]:
    """(result, executed Python line count) of fn() under sys.settrace."""
    lines = 0

    def _local(frame, event, arg):
        nonlocal lines
        if event == "line":
            lines += 1
        return _local

    def _global(frame, event, arg):
        return _local

    previous = sys.gettrace()
    sys.settrace(_global)
    try:
        result = fn()
    finally:
        sys.settrace(previous)
    return result, lines


def assert_linear(count_at: Callable[[int], int], n: int) -> None:
    """Doubling the input from n to 2n must less than triple the line count;
    a quadratic scan grows about four times."""
    half, full = count_at(n), count_at(2 * n)
    assert half > 1000, half
    assert full < 3 * half, (half, full)
