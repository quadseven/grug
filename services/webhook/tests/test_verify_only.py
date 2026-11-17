"""Findings that only ask the author to verify, ensure or confirm (grug#1749)."""
from __future__ import annotations

import pytest

from personas.code_reviewer.markings import build_review_result
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
from personas.code_reviewer.verify_only import is_summary_only, is_verify_only

# Fixed fixtures: the verify-only shape, with and without flavor.
VERIFY_ONLY = (
    "Ensure that the timeout is set for this call.",
    "Please verify that the new branch is covered by a test.",
    "Make sure the config key exists in every environment.",
    "Grug see new path. Consider confirming the cache is warmed first.",
    "You may want to double-check that the retry count is right.",
    "Check whether the caller handles an empty list.",
    "Ensure the helper never returns None.",
)

# Look-alikes: the word "ensure" or "verify" with a concrete failure, a
# cited line or a concrete input. None is demoted.
LOOK_ALIKES = (
    "`load` returns None when the file is missing. Ensure the caller handles it.",
    "Ensure the lock is released: on the error path at line 42 it is skipped.",
    "The loop drops the last row. Verify the bound.",
    "Ensure the parser accepts input `a,,b`; it raises IndexError today.",
    "When `count` is 0 the division crashes. Make sure to guard it.",
    "This uses `<` instead of `<=`, so the final page is never read.",
)


@pytest.mark.parametrize("message", VERIFY_ONLY)
def test_a_verify_only_message_is_detected(message):
    assert is_verify_only(message)


@pytest.mark.parametrize("message", LOOK_ALIKES)
def test_a_look_alike_with_evidence_is_not_detected(message):
    assert not is_verify_only(message)


def _finding(message, severity="medium", line=3):
    return Finding(
        file="app.py", line=line, severity=severity,
        rule_name="missing-validation", message=message, suggestion=None,
    )


@pytest.mark.parametrize("severity", ["low", "medium"])
def test_low_and_medium_verify_only_findings_are_summary_only(severity):
    assert is_summary_only(_finding(VERIFY_ONLY[0], severity))


@pytest.mark.parametrize("severity", ["high", "critical"])
def test_high_and_critical_are_never_demoted(severity):
    assert not is_summary_only(_finding(VERIFY_ONLY[0], severity))


def test_the_judge_rubric_grades_a_verify_only_ask_as_not_a_real_bug():
    from llm_client import _JUDGE_SYSTEM_PROMPT

    assert "only asks the author to verify, ensure or confirm" in _JUDGE_SYSTEM_PROMPT
    assert "is not a real bug (false)" in _JUDGE_SYSTEM_PROMPT


def test_the_review_posts_no_inline_comment_for_a_verify_only_finding():
    """The check summary is built from the full evaluation; only the inline
    review drops the verify-only finding."""
    ask = _finding(VERIFY_ONLY[0], "medium", line=3)
    real = _finding(LOOK_ALIKES[0], "medium", line=4)
    blocking = _finding(VERIFY_ONLY[1], "high", line=5)
    evaluation = CodeReviewEvaluation(findings=(ask, real, blocking), conclusion="failure")

    _, posted = build_review_result(evaluation, head_sha="abc123", event="COMMENT")

    assert [f.line for f in posted] == [4, 5]
