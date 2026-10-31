"""Durable retry for an async deep review whose GitHub publish failed (#738).

The deep review's expensive reasoner call has already succeeded by the time
`post_review` runs. When only that GitHub write fails on the wire (transport
error, 5xx, 429), the finished review is stored as a `PendingPublication` and
the poller cron retries it. See ADR-0025 for the scope decision (deep path
only) and the idempotency design summarized here:

- The dedup key is a digest of everything the review POST carries plus the
  install / repo / pull request it targets. It is computed before the first
  attempt and appended to the review body as a hidden HTML marker.
- Before every retry POST, the retry lists the pull request's reviews. A
  review whose body carries the key is the original attempt that succeeded
  on GitHub's side while the response was lost; the retry adopts its id
  instead of posting again.
- The store row is keyed by the same digest (a duplicate enqueue is a no-op)
  and claimed under a lease, so two pollers never work one row at once.

This module owns the bookkeeping: keys, (de)serialization, attempts, backoff
and the sweep. Talking to GitHub is the `deliver` callable the poller passes
in (`dispatch.deliver_pending_publication`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from adapters import pg_publish_retry_store as store
from github_reviews_client import InlineComment, ReviewResult
from llm_client import Backend, FindingOrigin

from personas.code_reviewer.persona import Finding

log = logging.getLogger(__name__)

PUBLISH_KEY_MARKER = "<!-- grug-publish:{} -->"
# Attempt n (1-based, counting only retries) waits min(60 * 2**n, 3600) s.
# Eight retries span about four hours, which outlasts a typical GitHub
# incident; the poller runs every ~15 min, so short waits round up to a tick.
MAX_RETRY_ATTEMPTS = 8
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600
# Longer than one delivery (a snapshot read, a review listing, a POST and a
# comment fetch, each with a 10 s timeout) so a live retry keeps its lease.
_LEASE_SECONDS = 300
_SWEEP_LIMIT = 20

DeliveryOutcome = Literal["posted", "already_posted", "superseded", "rejected"]


def publication_key(
    result: ReviewResult,
    *,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
) -> str:
    """The dedup key for one review POST: same target + same payload = same
    key, so the original attempt and every retry of it share one key."""
    material = json.dumps(
        {
            "installation_id": installation_id,
            "repo": f"{owner}/{repo_name}",
            "pull_number": pull_number,
            "review": asdict(result),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def with_publish_marker(result: ReviewResult, key: str) -> ReviewResult:
    """`result` with the key's hidden marker appended to its body."""
    return replace(result, body=f"{result.body}\n\n{PUBLISH_KEY_MARKER.format(key)}")


def review_carrying_key(reviews: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    """The already-posted review whose body carries `key`'s marker, if any."""
    marker = PUBLISH_KEY_MARKER.format(key)
    for review in reviews:
        if isinstance(review, dict) and marker in str(review.get("body") or ""):
            return review
    return None


@dataclass(frozen=True)
class PendingPublication:
    """Everything a retry needs to finish a deep review without the model:
    the review to post (marker already in its body), the findings it
    carries for comment capture, and the precomputed stack comment."""

    key: str
    installation_id: int
    owner: str
    repo_name: str
    pull_number: int
    head_sha: str
    result: ReviewResult
    findings: tuple[Finding, ...]
    review_span_context: dict[str, Any] | None
    author_login: str
    stack_body: str | None
    stack_create_if_absent: bool

    @property
    def pr(self) -> str:
        return f"{self.owner}/{self.repo_name}#{self.pull_number}"

    def to_record(self) -> dict[str, Any]:
        """JSON-safe form for the store. Backend is a str Enum, so its
        value serializes as the plain string."""
        return asdict(self)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> PendingPublication:
        raw_result = record["result"]
        result = ReviewResult(
            commit_id=raw_result["commit_id"],
            event=raw_result["event"],
            body=raw_result["body"],
            comments=tuple(InlineComment(**c) for c in raw_result["comments"]),
        )
        return cls(
            key=record["key"],
            installation_id=int(record["installation_id"]),
            owner=record["owner"],
            repo_name=record["repo_name"],
            pull_number=int(record["pull_number"]),
            head_sha=record["head_sha"],
            result=result,
            findings=tuple(_finding_from_record(f) for f in record["findings"]),
            review_span_context=record.get("review_span_context"),
            author_login=record.get("author_login") or "",
            stack_body=record.get("stack_body"),
            stack_create_if_absent=bool(record.get("stack_create_if_absent")),
        )


def _finding_from_record(raw: dict[str, Any]) -> Finding:
    origins = tuple(
        FindingOrigin(
            **{
                **origin,
                "backend": Backend(origin["backend"]),
                "evidence_paths": tuple(origin.get("evidence_paths") or ()),
            }
        )
        for origin in raw.get("origins") or ()
    )
    return Finding(**{**raw, "origins": origins})


def queue_publication(pending: PendingPublication) -> bool:
    """Store a failed publication for the poller to retry. True = queued
    now; False = the same key was already queued. Store errors propagate."""
    queued = store.enqueue_publish_retry(pending.key, pending.to_record())
    log.info(
        "elder_deep_publish_retry_queued",
        extra={"pr": pending.pr, "key": pending.key, "new": queued},
    )
    return queued


def backoff_seconds(attempts: int) -> int:
    """Wait before retry number `attempts` + 1."""
    return min(_BACKOFF_BASE_SECONDS * 2 ** attempts, _BACKOFF_CAP_SECONDS)


@dataclass
class SweepSummary:
    delivered: int = 0
    failed: int = 0
    abandoned: int = 0
    lost: int = 0


def retry_due_publications(
    deliver: Callable[[PendingPublication], DeliveryOutcome],
    *,
    limit: int = _SWEEP_LIMIT,
) -> SweepSummary:
    """Retry every due publication once. A failed retry stays queued with
    backoff until MAX_RETRY_ATTEMPTS, then is parked as abandoned. Store
    errors on the listing propagate; per-row failures never abort the
    sweep."""
    owner_token = uuid.uuid4().hex
    summary = SweepSummary()
    for key in store.list_due_publish_retries(limit):
        row = store.claim_publish_retry(
            key, owner_token=owner_token, lease_seconds=_LEASE_SECONDS,
        )
        if row is None:
            summary.lost += 1
            continue
        _retry_one(key, row, deliver, owner_token, summary)
    log.info("elder_deep_publish_retry_sweep", extra=asdict(summary))
    return summary


def _retry_one(
    key: str,
    row: dict[str, Any],
    deliver: Callable[[PendingPublication], DeliveryOutcome],
    owner_token: str,
    summary: SweepSummary,
) -> None:
    attempts = int(row.get("attempts") or 0) + 1
    try:
        pending = PendingPublication.from_record(row["publication"])
    except (KeyError, TypeError, ValueError, AssertionError) as error:
        # A row this code cannot read will not become readable on retry.
        log.error(
            "elder_deep_publish_retry_unreadable",
            extra={"key": key, "kind": type(error).__name__},
        )
        _defer(key, owner_token, attempts, f"unreadable: {type(error).__name__}", abandon=True)
        summary.abandoned += 1
        return
    try:
        outcome = deliver(pending)
    except Exception as error:  # noqa: BLE001 - any failure keeps the row queued
        _record_failure(key, pending, owner_token, attempts, error, summary)
        return
    if not store.complete_publish_retry(key, owner_token=owner_token):
        log.warning("elder_deep_publish_retry_lease_lost", extra={"pr": pending.pr, "key": key})
    log.info(
        "elder_deep_publish_retry_delivered",
        extra={"pr": pending.pr, "key": key, "outcome": outcome, "attempts": attempts},
    )
    summary.delivered += 1


def _record_failure(
    key: str,
    pending: PendingPublication,
    owner_token: str,
    attempts: int,
    error: Exception,
    summary: SweepSummary,
) -> None:
    abandon = attempts >= MAX_RETRY_ATTEMPTS
    _defer(key, owner_token, attempts, type(error).__name__, abandon=abandon)
    extra = {
        "pr": pending.pr, "key": key, "attempts": attempts,
        "kind": type(error).__name__,
    }
    if abandon:
        log.error("elder_deep_publish_retry_abandoned", extra=extra)
        summary.abandoned += 1
    else:
        log.warning("elder_deep_publish_retry_failed", extra=extra)
        summary.failed += 1


def _defer(key: str, owner_token: str, attempts: int, last_error: str, *, abandon: bool) -> None:
    if not store.defer_publish_retry(
        key,
        owner_token=owner_token,
        attempts=attempts,
        next_attempt_at=int(time.time()) + backoff_seconds(attempts),
        last_error=last_error,
        abandon=abandon,
    ):
        log.warning("elder_deep_publish_retry_lease_lost", extra={"key": key})
