"""Stable FIFO group identities shared by rerun queue producers."""

from __future__ import annotations

import hashlib


def _group_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def review_group_id(install_id: int, repo: str, pr_number: int) -> str:
    """Serialize normal Elder reviews per pull request."""
    return _group_id("elder-review-pr", install_id, repo, pr_number)


def rerun_group_id(
    install_id: int, repo: str, pr_number: int, persona: str,
) -> str:
    """Serialize explicit reruns per pull-request persona."""
    return _group_id("rerun-pr", install_id, repo, pr_number, persona)


def ask_group_id(install_id: int, repo: str, pr_number: int) -> str:
    """Serialize questions per PR without blocking unrelated workloads."""
    return _group_id("ask-pr", install_id, repo, pr_number)


def learn_group_id(install_id: int, repo: str, pr_number: int) -> str:
    """Serialize learnings classification per PR in its OWN group, so a slow
    classify never queues behind (or ahead of) a /grug ask for the same PR."""
    return _group_id("learn-pr", install_id, repo, pr_number)


class JobNotDue(Exception):
    """A queued job carries a `not_before` time still in the future.

    SQS FIFO queues cannot delay a single message, so a deferred job is
    re-enqueued at once and this is raised when it arrives early. The
    consumer answers by hiding the message for `delay_seconds` - one
    receive, not a failure: no redrive warning and no DLQ announcement."""

    def __init__(self, delay_seconds: float) -> None:
        super().__init__(f"job not due for {delay_seconds:.0f}s")
        self.delay_seconds = delay_seconds
