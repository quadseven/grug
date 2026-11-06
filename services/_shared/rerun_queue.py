"""The durable rerun queue job protocol (grug#584, ADR-0004).

This module is the single owner of what goes on `grug-rerun-jobs`: the
job vocabulary (kinds rerun / review / ask / learn), the one
`SCHEMA_VERSION`, a typed `encode_*` per kind, one `decode_job` for the
consumer, and the stable FIFO group and dedup identities. boto3 stays in
the producers' adapters: an encoder returns an `EncodedJob` whose
`message_kwargs()` feed `send_message` directly.

Wire compatibility. The API and webhook pods deploy at different times,
so the body each encoder writes is byte-identical to version 1 as it was
hand-built before this module existed (same keys, same order, a rerun job
still carries no `kind`). `test_rerun_queue_protocol.py` pins it. Bumping
`SCHEMA_VERSION` therefore needs the consumer to understand the new
version BEFORE any producer writes it.

Redrive decision for a malformed job. `decode_job` raises `InvalidJob`
(or its subclass `UnknownJobVersion` for a missing or unknown
`schema_version`) for any body it cannot turn into a typed job. The
consumer logs one structured error (`rerun_job_rejected`) and acks the
message: retrying cannot repair a body, so a redrive would only replay the
same failure `maxReceiveCount` times and dead-letter it, which is how the
#579 message poisoned the queue. The error log is the record.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final, Literal

SCHEMA_VERSION: Final = 1

JobKind = Literal["rerun", "review", "ask", "learn"]
KIND_RERUN: Final = "rerun"
KIND_REVIEW: Final = "review"
KIND_ASK: Final = "ask"
KIND_LEARN: Final = "learn"
_KINDS: Final = frozenset({KIND_RERUN, KIND_REVIEW, KIND_ASK, KIND_LEARN})

_SQS_DEDUP_ID_MAX_LEN: Final = 128


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


def _sha(prefix: str, material: str) -> str:
    return prefix + hashlib.sha256(material.encode("utf-8")).hexdigest()


def bounded_dedup_id(plain: str) -> str:
    """`plain` as the FIFO dedup id while it fits SQS's 128-char limit;
    past it (a long owner/repo name) a digest of the same content, so the
    send never fails validation and equal content still collapses."""
    if len(plain) <= _SQS_DEDUP_ID_MAX_LEN:
        return plain
    return _sha("rerun:", plain)


def review_dedup_id(
    install_id: int, repo: str, pr_number: int, requested_snapshot_id: str,
) -> str:
    """Bounded, full-snapshot FIFO dedup ID for a normal Elder review."""
    return _sha(
        "elder-review:",
        f"{install_id}\x1f{repo}\x1f{pr_number}\x1felder\x1f"
        f"{requested_snapshot_id}",
    )


@dataclass(frozen=True)
class EncodedJob:
    """One job ready for `send_message`: the JSON body plus its FIFO ids."""

    body: str
    group_id: str
    dedup_id: str

    def message_kwargs(self) -> dict[str, str]:
        return {
            "MessageBody": self.body,
            "MessageGroupId": self.group_id,
            "MessageDeduplicationId": self.dedup_id,
        }


def encode_rerun(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    persona: str,
    not_before: float = 0.0,
    requeue_of: str = "",
    dedup_key: str = "",
    comment_id: int = 0,
    queued_at: float = 0.0,
    hold_started_at: float = 0.0,
) -> EncodedJob:
    """An explicit persona rerun. Dedup on `(install, repo, pr, persona)`
    with no head sha: a rerun always targets the PR's current head, so two
    clicks are the same job. A command (`comment_id`), a `dedup_key` or a
    deferral (`not_before`) each get their own hashed dedup id, because the
    plain id may still sit inside SQS's 5-minute window and a collapsed
    send would lose the job. The caller stamps `queued_at` for a command."""
    job: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "install_id": install_id,
        "repo": repo,
        "pr_number": pr_number,
        "persona": persona,
    }
    if comment_id:
        job["comment_id"] = int(comment_id)
        job["queued_at"] = float(queued_at)
        if hold_started_at:
            job["hold_started_at"] = float(hold_started_at)
        dedup_key = dedup_key or f"cmd{int(comment_id)}"
    plain = f"{install_id}:{repo}:{pr_number}:{persona}"
    if not_before > 0:
        job["not_before"] = float(not_before)
        material = f"{plain}\x1fnotbefore{int(not_before)}"
        if requeue_of:
            material += f"\x1frequeue{requeue_of}"
        if dedup_key:
            material += f"\x1fkey{dedup_key}"
        dedup_id = _sha("rerun:", material)
    elif dedup_key:
        dedup_id = _sha("rerun:", f"{plain}\x1fkey{dedup_key}")
    else:
        dedup_id = bounded_dedup_id(plain)
    return EncodedJob(
        body=json.dumps(job),
        group_id=rerun_group_id(install_id, repo, pr_number, persona),
        dedup_id=dedup_id,
    )


def encode_review(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    requested_head_sha: str,
    requested_snapshot_id: str,
    settle_seconds: int,
    redrive_count: int = 0,
) -> EncodedJob:
    """A normal Elder review, deduplicated on the complete review input."""
    body = json.dumps({
        "schema_version": SCHEMA_VERSION,
        "kind": KIND_REVIEW,
        "install_id": install_id,
        "repo": repo,
        "pr_number": pr_number,
        "persona": "elder",
        "requested_head_sha": requested_head_sha,
        "requested_snapshot_id": requested_snapshot_id,
        "settle_seconds": settle_seconds,
        "redrive_count": redrive_count,
    })
    return EncodedJob(
        body=body,
        group_id=review_group_id(install_id, repo, pr_number),
        dedup_id=review_dedup_id(
            install_id, repo, pr_number, requested_snapshot_id,
        ),
    )


def encode_ask(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    comment_id: int,
    question: str,
    reply_to_comment_id: int = 0,
) -> EncodedJob:
    """A `/grug ask` question, deduplicated per comment."""
    body = json.dumps({
        "schema_version": SCHEMA_VERSION, "kind": KIND_ASK,
        "install_id": install_id, "repo": repo, "pr_number": pr_number,
        "comment_id": comment_id, "question": question,
        "reply_to_comment_id": reply_to_comment_id,
    })
    return EncodedJob(
        body=body,
        group_id=ask_group_id(install_id, repo, pr_number),
        dedup_id=bounded_dedup_id(
            f"{install_id}:{repo}:{pr_number}:ask:{comment_id}",
        ),
    )


def encode_learn(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    comment_id: int,
    parent_comment_id: int,
    reply_text: str,
    author: str = "",
    defer_count: int = 0,
    not_before: float = 0.0,
    first_deferred_at: float = 0.0,
    requeue_of: str = "",
) -> EncodedJob:
    """A learnings classification job. Each deferral and each requeue gets
    a distinct dedup id so a send inside the 5-minute window is not lost."""
    job: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "kind": KIND_LEARN,
        "install_id": install_id, "repo": repo, "pr_number": pr_number,
        "comment_id": comment_id, "parent_comment_id": parent_comment_id,
        "reply_text": reply_text, "author": author,
    }
    material = f"{install_id}\x1f{repo}\x1f{pr_number}\x1f{comment_id}"
    if defer_count > 0:
        job.update({
            "defer_count": defer_count, "not_before": not_before,
            "first_deferred_at": first_deferred_at,
        })
        material += f"\x1fdefer{defer_count}"
    if requeue_of:
        material += f"\x1frequeue{requeue_of}"
    return EncodedJob(
        body=json.dumps(job),
        group_id=learn_group_id(install_id, repo, pr_number),
        dedup_id=_sha("learn:", material),
    )


class InvalidJob(ValueError):
    """A queue body that cannot be decoded into a job. See the module
    docstring for the redrive decision: log and ack, never retry."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnknownJobVersion(InvalidJob):
    """The body's `schema_version` is missing or not one this code reads."""


@dataclass(frozen=True)
class QueueJob:
    """A validated job. `payload` is the full decoded body, which the kind
    handlers read for their own optional fields."""

    kind: JobKind
    install_id: int
    repo: str
    pr_number: int
    persona: str
    payload: dict[str, Any]


def _positive_int(job: dict[str, Any], key: str) -> int:
    raw = job.get(key)
    if isinstance(raw, bool):
        raise InvalidJob(f"{key} is not an integer")
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise InvalidJob(f"{key} is not an integer") from None
    if value <= 0:
        raise InvalidJob(f"{key} is not positive")
    return value


def decode_job(body: str | bytes) -> QueueJob:
    """Validate one queue body and return the typed job, or raise
    `InvalidJob` (`UnknownJobVersion` for the version check)."""
    try:
        job = json.loads(body)
    except (TypeError, ValueError):
        raise InvalidJob("body is not JSON") from None
    if not isinstance(job, dict):
        raise InvalidJob("body is not a JSON object")
    version = job.get("schema_version")
    if "schema_version" not in job:
        raise UnknownJobVersion("schema_version is missing")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise UnknownJobVersion(f"schema_version {version!r} is not supported")
    kind = job.get("kind", KIND_RERUN)
    if kind not in _KINDS:
        raise InvalidJob(f"kind {str(kind)[:40]!r} is unknown")
    install_id = _positive_int(job, "install_id")
    pr_number = _positive_int(job, "pr_number")
    repo = job.get("repo")
    if not isinstance(repo, str):
        raise InvalidJob("repo is not owner/name")
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise InvalidJob("repo is not owner/name")
    persona = job.get("persona", "elder")
    if not isinstance(persona, str) or not persona:
        raise InvalidJob("persona is not a name")
    return QueueJob(
        kind=kind, install_id=install_id, repo=repo, pr_number=pr_number,
        persona=persona, payload=job,
    )


class JobNotDue(Exception):
    """A queued job carries a `not_before` time still in the future.

    SQS FIFO queues cannot delay a single message, so a deferred job is
    re-enqueued at once and this is raised when it arrives early. The
    consumer answers by hiding the message for `delay_seconds` - one
    receive, not a failure: no redrive warning and no DLQ announcement."""

    def __init__(self, delay_seconds: float) -> None:
        super().__init__(f"job not due for {delay_seconds:.0f}s")
        self.delay_seconds = delay_seconds
