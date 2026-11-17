"""grug#1753: a neutral Elder check must say why it is not a pass.

A clean-looking review used to publish with the title "Elder clear - no
markings" and a `neutral` conclusion, with no word about advisory mode or
the degradation that held it back. Neither the author nor the lane could
tell what would turn it into a pass. Every neutral Elder check now carries
a standalone "Neutral because ..." line naming the exact cause.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "services" / "_shared"))

from personas.code_reviewer.markings import _lead_if_degraded, _summary_markdown  # noqa: E402
from personas.code_reviewer.persona import CodeReviewEvaluation  # noqa: E402
from personas.code_reviewer.pinned_review import pin_review  # noqa: E402
from personas.review_publish import (  # noqa: E402
    LOW_CONFIDENCE,
    append_neutral_reason,
    neutral_check_title,
    neutral_reason,
    plan_publish,
)

_SHA = "a" * 40


def _evaluation(*, degraded_reason: str | None = None) -> CodeReviewEvaluation:
    return CodeReviewEvaluation(
        findings=(),
        conclusion="neutral" if degraded_reason else "success",
        degraded_reason=degraded_reason,
    )


def _published(
    evaluation: CodeReviewEvaluation, *, mode: str = "advisory",
    unreviewed_lock_files: tuple[str, ...] = (),
    living_baseline_rejected: str = "",
):
    """The (conclusion, title, summary) the neutral-reason helpers produce
    over a rendered, pinned summary.

    This exercises the helpers only. The wiring inside dispatch is pinned by
    the dispatch-driven tests: services/webhook/tests/test_pinned_review.py
    and test_living_hunt.py for Tier-1, test_async_deep.py for the deep
    append.
    """
    title, summary = _summary_markdown(
        evaluation, living_baseline_rejected=living_baseline_rejected,
    )
    summary = pin_review(summary, evaluation, head_sha=_SHA)
    summary = _lead_if_degraded(summary, evaluation)
    outcome = plan_publish(evaluation, mode=mode, degraded_fails_closed=True)
    if outcome.conclusion == "neutral":
        why = neutral_reason(
            evaluation, mode=mode,
            unreviewed_lock_files=unreviewed_lock_files,
        )
        summary = append_neutral_reason(summary, why)
        title = neutral_check_title(title, why)
    return outcome.conclusion, title, summary


def test_low_confidence_neutral_summary_names_the_reason():
    evaluation = _evaluation(degraded_reason=LOW_CONFIDENCE)

    # The old wording says "low confidence" in prose but never names the
    # `degraded_reason` value, so a reader cannot tell which degradation it was.
    plain_title, plain_summary = _summary_markdown(evaluation)
    assert LOW_CONFIDENCE not in plain_title
    assert LOW_CONFIDENCE not in plain_summary

    conclusion, title, summary = _published(evaluation)
    assert conclusion == "neutral"
    assert LOW_CONFIDENCE in summary
    neutral_lines = [line for line in summary.splitlines()
                     if line.startswith("Neutral because ")]
    assert len(neutral_lines) == 1
    assert LOW_CONFIDENCE in neutral_lines[0]
    assert "Neutral because" in title


def test_advisory_clean_review_names_advisory_mode():
    """The path that produced the neutral on the reported pull request: a
    clean evaluation, no degradation, forced neutral by advisory mode."""
    conclusion, title, summary = _published(_evaluation(), mode="advisory")
    assert conclusion == "neutral"
    assert "Elder clear" in title
    assert "advisory mode" in summary
    assert "advisory mode" in title
    assert any(line.startswith("Neutral because ")
               for line in summary.splitlines())


def test_clean_full_diff_after_rejected_baseline_is_success():
    """A rejected Living Hunt baseline only adds a summary note. A clean
    full-diff review still concludes success in blocking mode; it is not
    itself a reason for neutral."""
    evaluation = _evaluation()
    conclusion, title, summary = _published(
        evaluation, mode="blocking", living_baseline_rejected="prior000",
    )
    assert conclusion == "success"
    assert "reviewed the full PR diff instead" in summary
    assert not any(line.startswith("Neutral because ")
                   for line in summary.splitlines())
    assert not title.startswith("Neutral because ")


def test_no_diff_neutral_names_no_diff_not_advisory_mode():
    """`no_diff` keeps a blocking-mode check neutral, yet it is not a
    degraded review and not advisory mode, so the reason names it."""
    conclusion, title, summary = _published(
        _evaluation(degraded_reason="no_diff"), mode="blocking",
    )
    assert conclusion == "neutral"
    [line] = [line for line in summary.splitlines()
              if line.startswith("Neutral because ")]
    assert "`no_diff`" in line
    assert "no reviewable diff" in line
    assert "advisory" not in line and "degraded" not in line


def test_a_neutral_with_no_named_cause_does_not_claim_advisory_mode():
    """A neutral the gate did not explain (for example one carried into the
    check by a later step) must not be reported as advisory mode."""
    why = neutral_reason(
        CodeReviewEvaluation(findings=(), conclusion="neutral"), mode="blocking",
    )
    assert "advisory" not in why


def test_unreviewed_lock_files_are_named_as_the_reason():
    why = neutral_reason(
        _evaluation(), mode="blocking",
        unreviewed_lock_files=("uv.lock", "web/package-lock.json"),
    )
    assert "uv.lock" in why and "web/package-lock.json" in why
    published = append_neutral_reason("Review body.", why)
    assert published.splitlines()[-1] == f"Neutral because {why}."
