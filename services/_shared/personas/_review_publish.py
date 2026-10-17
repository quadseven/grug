"""Stable shared API for persona review fetch and publish helpers."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Literal

from github_checks_client import CheckConclusion
from github_reviews_client import ReviewEvent
from personas.code_reviewer.persona import CodeReviewEvaluation

ReviewMode = Literal["advisory", "blocking"]
PersonaResult = Literal[
    "pass", "fail", "skipped", "publish_failed", "review_rejected",
    "unhandled_error",
]


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """Resolved check/review publication state used by logs and activity rows."""

    conclusion: CheckConclusion
    event: ReviewEvent
    check_failed: bool
    review_failed: bool = False
    review_rejected: bool = False

    def result_for(self, evaluation: CodeReviewEvaluation) -> PersonaResult:
        if self.check_failed or self.review_failed:
            return "publish_failed"
        if evaluation.degraded_reason:
            return "skipped"
        if self.review_rejected:
            return "review_rejected"
        return "pass" if evaluation.passed else "fail"

    def degraded_reason_for(self, evaluation: CodeReviewEvaluation) -> str | None:
        return evaluation.degraded_reason or (
            "check_publish_failed" if self.check_failed else None
        )


def publish_shape(
    evaluation: CodeReviewEvaluation, *, mode: ReviewMode,
) -> tuple[CheckConclusion, ReviewEvent]:
    """Return aligned check and review actions for one evaluation."""
    if mode == "advisory" or evaluation.degraded_reason:
        return "neutral", "COMMENT"
    if evaluation.conclusion == "failure":
        return "failure", "REQUEST_CHANGES"
    return evaluation.conclusion, "COMMENT"


def publish_outcome(
    evaluation: CodeReviewEvaluation, *, mode: ReviewMode,
    check_failed: bool, review_failed: bool = False,
    review_rejected: bool = False,
) -> PublishOutcome:
    conclusion, event = publish_shape(evaluation, mode=mode)
    return PublishOutcome(conclusion, event, check_failed, review_failed, review_rejected)


def resolve_result(
    evaluation: CodeReviewEvaluation, *, check_publish_failed: bool,
    review_publish_failed: bool = False, review_publish_rejected: bool = False,
) -> PersonaResult:
    """Compatibility resolver for callers that have not retained an outcome."""
    return PublishOutcome(
        "neutral", "COMMENT", check_publish_failed,
        review_publish_failed, review_publish_rejected,
    ).result_for(evaluation)


# Compatibility facade keeps implementations and their dependencies in the
# code-reviewer module while callers use this stable, public import surface.
def fetch_pr_diff(*args, **kwargs):
    return getattr(import_module("personas.code_reviewer.dispatch"), "_fetch_pr_diff")(*args, **kwargs)


def fetch_file_contents(*args, **kwargs):
    return getattr(import_module("personas.code_reviewer.dispatch"), "_fetch_file_contents")(*args, **kwargs)


def prior_finding_keys(*args, **kwargs):
    return getattr(import_module("personas.code_reviewer.dispatch"), "_prior_finding_keys")(*args, **kwargs)


def build_review_result(*args, **kwargs):
    return getattr(import_module("personas.code_reviewer.dispatch"), "_build_review_result")(*args, **kwargs)


def severity_chip(*args, **kwargs):
    return getattr(import_module("personas.code_reviewer.dispatch"), "_severity_chip")(*args, **kwargs)


__all__ = [
    "PersonaResult", "PublishOutcome", "ReviewMode", "build_review_result",
    "fetch_file_contents", "fetch_pr_diff", "prior_finding_keys",
    "publish_outcome", "publish_shape", "resolve_result", "severity_chip",
]
