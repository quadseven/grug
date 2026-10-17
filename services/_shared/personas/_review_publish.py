"""Stable shared interface for persona review fetch and publish helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from github_checks_client import CheckConclusion
from github_reviews_client import ReviewEvent

__all__ = [
    "PublishOutcome", "ReviewMode", "build_review_result", "fetch_file_contents",
    "fetch_pr_diff", "prior_finding_keys", "publish_shape", "resolve_result",
    "severity_chip",
]

ReviewMode = Literal["advisory", "blocking"]


@dataclass(frozen=True)
class PublishOutcome:
    conclusion: CheckConclusion
    event: ReviewEvent
    check_publish_failed: bool = False
    review_publish_failed: bool = False
    review_publish_rejected: bool = False

    @property
    def result(self) -> str:
        if self.check_publish_failed or self.review_publish_failed:
            return "publish_failed"
        if self.review_publish_rejected:
            return "review_rejected"
        return "pass"

    def activity_degraded_reason(self, evaluation_reason: str | None) -> str | None:
        return evaluation_reason or ("check_publish_failed" if self.check_publish_failed else None)


def _dispatch(name: str, *args: object, **kwargs: object):
    from personas.code_reviewer import dispatch
    return getattr(dispatch, name)(*args, **kwargs)


def build_review_result(*args, **kwargs):
    return _dispatch("_build_review_result", *args, **kwargs)


def fetch_file_contents(*args, **kwargs):
    return _dispatch("_fetch_file_contents", *args, **kwargs)


def fetch_pr_diff(*args, **kwargs):
    return _dispatch("_fetch_pr_diff", *args, **kwargs)


def prior_finding_keys(*args, **kwargs):
    return _dispatch("_prior_finding_keys", *args, **kwargs)


def publish_shape(evaluation, *, mode: ReviewMode) -> tuple[CheckConclusion, ReviewEvent]:
    return _dispatch("_publish_shape", evaluation, mode=mode)


def resolve_result(evaluation, outcome: PublishOutcome | None = None, **publish_state) -> str:
    if outcome is None:
        outcome = PublishOutcome(
            conclusion="neutral", event="COMMENT", **publish_state,
        )
    return _dispatch(
        "_resolve_result", evaluation,
        check_publish_failed=outcome.check_publish_failed,
        review_publish_failed=outcome.review_publish_failed,
        review_publish_rejected=outcome.review_publish_rejected,
    )


def severity_chip(severity: str) -> str:
    return _dispatch("_severity_chip", severity)
