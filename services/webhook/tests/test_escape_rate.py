"""#1568 slice 5: weekly escapes per 100 merged PRs, agent vs human."""

from __future__ import annotations

import pytest

from escape_rate import AGENT, HUMAN, MergedPull, escape_rates


def _pulls():
    return [MergedPull(1, AGENT), MergedPull(2, AGENT), MergedPull(3, AGENT),
            MergedPull(4, AGENT), MergedPull(5, HUMAN), MergedPull(6, HUMAN)]


def test_rate_is_split_by_author_kind_of_the_escaped_pr():
    rates = escape_rates(_pulls(), [1, 5])
    a, h = rates[AGENT], rates[HUMAN]
    assert (a.merged, a.escapes, a.per_100) == (4, 1, 25.0)
    assert (h.merged, h.escapes, h.per_100) == (2, 1, 50.0)


def test_one_pr_corrected_twice_counts_once():
    assert escape_rates(_pulls(), [1, 1, 1])[AGENT].escapes == 1


def test_escape_of_a_pr_outside_the_window_is_ignored():
    assert escape_rates(_pulls(), [999])[AGENT].escapes == 0


def test_no_merged_pulls_gives_no_rate_not_zero():
    rates = escape_rates([MergedPull(1, AGENT)], [])
    assert rates[HUMAN].per_100 is None
    assert rates[AGENT].per_100 == 0.0


def test_unknown_author_kind_is_rejected():
    with pytest.raises(ValueError):
        escape_rates([MergedPull(1, "bot")], [])
