"""Behavior tests for Elder's bounded review planner."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from review_pipeline import plan_review, render_review_map


@dataclass(frozen=True)
class _Hunk:
    path: str
    body: str


def test_small_diff_stays_on_single_review_path() -> None:
    plan = plan_review(
        [_Hunk("src/a.py", "a" * 20), _Hunk("tests/test_a.py", "b" * 20)],
        max_cohort_chars=100,
    )

    assert not plan.staged
    assert plan.cohorts[0].hunk_indexes == (0, 1)
    assert render_review_map(plan) == ""


def test_large_diff_keeps_related_directories_together() -> None:
    hunks = [
        _Hunk("src/a.py", "a" * 40),
        _Hunk("tests/test_a.py", "t" * 40),
        _Hunk("src/b.py", "b" * 40),
    ]

    plan = plan_review(hunks, max_cohort_chars=90)

    assert plan.staged
    assert [cohort.label for cohort in plan.cohorts] == ["src", "tests"]
    assert plan.cohorts[0].hunk_indexes == (0, 2)
    assert plan.cohorts[1].hunk_indexes == (1,)


def test_oversized_area_is_split_without_splitting_hunks() -> None:
    hunks = [
        _Hunk("src/a.py", "a" * 60),
        _Hunk("src/b.py", "b" * 60),
        _Hunk("src/c.py", "c" * 120),
    ]

    plan = plan_review(hunks, max_cohort_chars=100)

    assert [cohort.hunk_indexes for cohort in plan.cohorts] == [(0,), (1,), (2,)]
    assert plan.cohorts[-1].diff_chars == 120
    assert not plan.cohorts[0].oversized
    assert plan.cohorts[-1].oversized


def test_review_map_shares_structure_but_not_diff_content() -> None:
    plan = plan_review(
        [_Hunk("src/a.py", "SECRET-DIFF" * 5), _Hunk("docs/readme.md", "x" * 60)],
        max_cohort_chars=60,
    )

    rendered = render_review_map(plan)

    assert "Cohort 1" in rendered
    assert "src/a.py" in rendered
    assert "docs/readme.md" in rendered
    assert "SECRET-DIFF" not in rendered


def test_invalid_cohort_budget_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        plan_review([_Hunk("x.py", "x")], max_cohort_chars=0)


def test_many_small_files_are_bounded_by_path_count() -> None:
    hunks = [_Hunk(f"src/file_{index}.py", "x") for index in range(5)]

    plan = plan_review(hunks, max_cohort_chars=100, max_cohort_paths=2)

    assert [len(cohort.paths) for cohort in plan.cohorts] == [2, 2, 1]
