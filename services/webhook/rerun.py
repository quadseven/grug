# WEBHOOK-ONLY (NOT mirrored): the SQS consumer for operator-triggered re-runs
# (#305, ADR-0004). The api service ENQUEUES (services/api/rerun.py); only the
# webhook image carries the persona-dispatch + GitHub-App machinery, so the
# consumer lives here — same split as the cave fallback (cave_fallback.py).
"""Re-run consumer (#305, ADR-0004) — grug's backfill for a dropped/`errored`
review.

`consumer.py` long-polls `grug-rerun-jobs.fifo` and routes each batch here. For each job the
consumer fetches the PR's **current** head + diff and re-runs the named persona
via the unchanged `dispatch_code_review`, which posts the check-run and upserts
the `CheckVerdictRecord` — healing the `errored` row in place if the head is
unchanged, appending a fresh row if the PR moved on.

Failure semantics differ from the cave result handler ON PURPOSE: a transient
infra failure (GitHub 5xx, fetch error) **raises** so the consumer retries via
the visibility timeout and, after `maxReceiveCount`, lands in the DLQ - the
operator-visible "this re-run is stuck" signal. Durable quiet-window reviews
also redrive partial/model/publish failures; explicit operator reruns preserve
the historical published-neutral completion behavior.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import boto3
import httpx

from adapters.install_store import get_repo_config  # type: ignore
from github_app_auth import with_install_token_retry
from personas.code_reviewer.dispatch import dispatch_code_review
from personas.code_reviewer.snapshot import (
    review_snapshot_id,
    review_snapshot_id_from_pr,
)
from personas.guard.dispatch import dispatch_guard_review
from personas.smasher.dispatch import dispatch_smasher_review
from personas.walkthrough.dispatch import dispatch_walkthrough_review
from rerun_personas import (
    GUARD as _GUARD,
    RERUNNABLE as _RERUNNABLE,
    SMASHER as _SMASHER,
    TELLER as _TELLER,
)
from rerun_queue import (
    ask_group_id as _ask_group_id,
    rerun_group_id as _rerun_group_id,
    review_group_id as _review_group_id,
)

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.rerun")

_GH_API = "https://api.github.com"
_FETCH_TIMEOUT = 15.0

_sqs = boto3.client("sqs")
# Queue URL injected by Pulumi (same env the consumer reads). Unset in
# local/dev/tests -> enqueue raises, surfaced as best-effort by the caller.
_RERUN_QUEUE_URL = os.getenv("GRUG_RERUN_QUEUE_URL", "")
SCHEMA_VERSION = 1
_MAX_SETTLE_SECONDS = 300
# The lease matches the queue's fallback visibility timeout and is renewed on
# the same cadence as the SQS visibility heartbeat while a review is active.
_REVIEW_CLAIM_LEASE_SECONDS = 900
_REVIEW_CLAIM_HEARTBEAT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class _ReviewClaimHeartbeat:
    stop: threading.Event
    ownership_lost: threading.Event
    thread: threading.Thread


def _review_dedup_id(
    install_id: int, repo: str, pr_number: int, requested_snapshot_id: str,
) -> str:
    """Bounded, full-snapshot FIFO dedup ID."""
    material = (
        f"{install_id}\x1f{repo}\x1f{pr_number}\x1felder\x1f"
        f"{requested_snapshot_id}"
    )
    return f"elder-review:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def enqueue_rerun(*, install_id: int, repo: str, pr_number: int, persona: str) -> None:
    """Send a `RerunJob` to `grug-rerun-jobs` (the webhook-side producer used by
    Elder self-recovery, #418). Same job shape + FIFO dedup as the api producer:
    content-dedup on `(install, repo, pr, persona)` over the 5-min window, so a
    self-recover enqueue that races an operator re-run (or a second drop) for the
    same PR collapses to one job. `head_sha` is NOT in the key - a re-run always
    targets the PR's CURRENT head. Raises `RuntimeError` when the queue isn't
    configured (the caller treats enqueue as best-effort)."""
    if not _RERUN_QUEUE_URL:
        raise RuntimeError("GRUG_RERUN_QUEUE_URL not configured")
    _sqs.send_message(
        QueueUrl=_RERUN_QUEUE_URL,
        MessageBody=json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "install_id": install_id,
                "repo": repo,
                "pr_number": pr_number,
                "persona": persona,
            }
        ),
        MessageGroupId=_rerun_group_id(
            install_id, repo, pr_number, persona,
        ),
        MessageDeduplicationId=f"{install_id}:{repo}:{pr_number}:{persona}",
    )
    log.info(
        "rerun_enqueued",
        extra={"install_id": install_id, "repo": repo, "pr": pr_number, "persona": persona},
    )


def enqueue_review(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    requested_base_sha: str,
    requested_head_sha: str,
    requested_title: str,
    requested_body: str,
    settle_seconds: int,
) -> None:
    """Enqueue one normal Elder review on the durable consumer lane.

    This differs from an operator/self-recovery rerun: FIFO dedup covers the
    complete review input, not only the head. The consumer still fetches the
    current PR before and after settling, so queued title/body text is never
    trusted as the source of review evidence.
    """
    if not _RERUN_QUEUE_URL:
        raise RuntimeError("GRUG_RERUN_QUEUE_URL not configured")
    if not requested_head_sha:
        raise ValueError("requested_head_sha must be non-empty")
    requested_snapshot_id = review_snapshot_id(
        base_sha=requested_base_sha,
        head_sha=requested_head_sha,
        title=requested_title,
        body=requested_body,
    )
    settle = min(_MAX_SETTLE_SECONDS, max(0, int(settle_seconds)))
    _sqs.send_message(
        QueueUrl=_RERUN_QUEUE_URL,
        MessageBody=json.dumps({
            "schema_version": SCHEMA_VERSION,
            "kind": "review",
            "install_id": install_id,
            "repo": repo,
            "pr_number": pr_number,
            "persona": "elder",
            "requested_head_sha": requested_head_sha,
            "requested_snapshot_id": requested_snapshot_id,
            "settle_seconds": settle,
        }),
        MessageGroupId=_review_group_id(install_id, repo, pr_number),
        MessageDeduplicationId=_review_dedup_id(
            install_id, repo, pr_number, requested_snapshot_id,
        ),
    )
    log.info(
        "elder_review_enqueued",
        extra={
            "install_id": install_id,
            "repo": repo,
            "pr": pr_number,
            "head_sha": requested_head_sha[:8],
            "snapshot_id": requested_snapshot_id[:11],
            "settle_seconds": settle,
        },
    )

# Persona rerun sets + dispatch-routing groups come from the shared
# rerun_personas module (imported at the top) so the API request validator and
# this consumer cannot drift (#581): a rerunnable persona the request rejects
# would be a dead capability - exactly the Teller bug that motivated the split.
# The static TPM check is requestable but logged + skipped below (deliberate).


def enqueue_ask(*, install_id: int, repo: str, pr_number: int, comment_id: int, question: str) -> None:
    """Enqueue a `/grug ask` job (#528) so the heavy LLM Q&A runs in the
    consumer, NOT inline in the webhook ACK path. Dedup keys on comment_id
    (each question is distinct - unlike a persona rerun), so a re-delivered
    comment collapses but two different questions do not."""
    if not _RERUN_QUEUE_URL:
        raise RuntimeError("GRUG_RERUN_QUEUE_URL not configured")
    _sqs.send_message(
        QueueUrl=_RERUN_QUEUE_URL,
        MessageBody=json.dumps({
            "schema_version": SCHEMA_VERSION, "kind": "ask",
            "install_id": install_id, "repo": repo, "pr_number": pr_number,
            "comment_id": comment_id, "question": question,
        }),
        MessageGroupId=_ask_group_id(install_id, repo, pr_number),
        MessageDeduplicationId=f"{install_id}:{repo}:{pr_number}:ask:{comment_id}",
    )
    log.info("ask_enqueued", extra={"install_id": install_id, "repo": repo,
                                    "pr": pr_number, "comment_id": comment_id})


def _gh_get(token: str, url: str) -> dict[str, Any]:
    resp = httpx.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _gh_get_text(token: str, url: str, *, accept: str) -> str:
    resp = httpx.get(
        url, headers={"Authorization": f"Bearer {token}", "Accept": accept},
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def _gh_post(token: str, url: str, json_body: dict[str, Any]) -> None:
    resp = httpx.post(
        url, json=json_body,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()


def _run_ask(install_id: int, repo_full: str, pr_number: int, question: str) -> str:
    """Answer a /grug ask question in the consumer (async, #528). Fetches the
    diff, runs the JSON-constrained Q&A over the REDACTED question + diff, and
    posts the answer as a reply. Records an activity row. Never raises past
    the job (a bad answer degrades to a fallback reply)."""
    from urllib.parse import quote as _q
    from llm_client import _redact_secrets, answer_pr_question  # type: ignore
    from observability import emit_gauge  # type: ignore
    owner, _, repo_name = repo_full.partition("/")
    q = _redact_secrets(question)

    def _do(token: str) -> str:
        diff = _gh_get_text(
            token,
            f"{_GH_API}/repos/{_q(owner, safe='')}/{_q(repo_name, safe='')}/pulls/{pr_number}",
            accept="application/vnd.github.v3.diff",
        )
        answer = answer_pr_question(q, diff, install_id)
        body = (f"{answer}\n\n*(Grug answered from the PR diff - may be wrong; verify.)*"
                if answer else
                "Grug could not answer that right now (the thinking-rock is tired). Try again.")
        _gh_post(
            token,
            f"{_GH_API}/repos/{_q(owner, safe='')}/{_q(repo_name, safe='')}/issues/{pr_number}/comments",
            {"body": body},
        )
        return "answered" if answer else "ask_no_answer"
    result = with_install_token_retry(install_id, _do)
    try:
        emit_gauge("grug.interactive.ask", 1)
    except Exception:  # noqa: BLE001
        pass
    log.info("ask_answered", extra={"repo": repo_full, "pr": pr_number, "result": result})
    return result


def _run_one(body: str) -> str:
    """Re-run ONE job. Raises on a malformed message or an infra fetch failure
    (→ ESM retry → DLQ). Returns a short status for the batch summary log.

    Keyed by `repo` ("owner/name") — what the Activity row (the trigger) carries
    — NOT a repo_id; the repo_id (for the RepoConfig lookup) is derived from the
    PR's `base.repo.id` in the same fetch."""
    job = json.loads(body)  # malformed → JSONDecodeError → retry → DLQ
    install_id = int(job["install_id"])
    repo_full = str(job["repo"])  # "owner/name"
    pr_number = int(job["pr_number"])
    if job.get("kind") == "ask":
        return _run_ask(install_id, repo_full, pr_number, str(job.get("question", "")))
    if job.get("kind") == "review":
        return _run_hot_review(job, install_id, repo_full, pr_number)
    persona = str(job.get("persona", "elder"))

    if persona not in _RERUNNABLE:
        # Not an infra failure — don't retry/DLQ a persona we don't drive yet.
        log.info(
            "rerun_unsupported_persona",
            extra={"persona": persona, "repo": repo_full, "pr": pr_number},
        )
        return "skipped_persona"

    owner, _, repo_name = repo_full.partition("/")
    # Fetch the PR's CURRENT head (+ the repo id, for RepoConfig). A 5xx/
    # RequestError raises → ESM retry → DLQ. with_install_token_retry refreshes
    # a stale token once.
    pr = with_install_token_retry(
        install_id,
        lambda tok: _gh_get(
            tok, f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo_name, safe='')}/pulls/{pr_number}"
        ),
    )
    repo_id = int(pr["base"]["repo"]["id"])

    payload = _review_payload(
        install_id=install_id,
        owner=owner,
        repo_name=repo_name,
        pr_number=pr_number,
        pr=pr,
        action="rerun",
    )
    cfg = get_repo_config(install_id, repo_id)
    # Neither dispatch raises a wire exception: each fetches the diff, re-runs
    # its persona, publishes, and upserts the verdict (heal-in-place on an
    # unchanged head, append on a moved-on PR). A repeat outage degrades to a
    # published `errored` row — the job still completed.
    if persona in _GUARD:
        dispatch_guard_review(
            payload, blocking=bool(cfg.get("guard_blocking", False)),
        )
    elif persona in _SMASHER:
        # Smasher is advisory-only (no blocking flag); the global master switch
        # is re-checked inside dispatch_smasher_review.
        dispatch_smasher_review(payload, blocking=False)
    elif persona in _TELLER:
        # Teller has no blocking mode (comment-only, no blocking_flag).
        dispatch_walkthrough_review(payload, blocking=False)
    else:
        dispatch_code_review(
            payload, blocking=bool(cfg.get("code_reviewer_blocking", False)),
        )
    log.info(
        "rerun_dispatched",
        extra={"repo": f"{owner}/{repo_name}", "pr": pr_number, "persona": persona},
    )
    return "dispatched"


def _fetch_current_pr(
    install_id: int, owner: str, repo_name: str, pr_number: int,
) -> dict[str, Any]:
    return with_install_token_retry(
        install_id,
        lambda tok: _gh_get(
            tok,
            f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo_name, safe='')}/pulls/{pr_number}",
        ),
    )


def _review_payload(
    *,
    install_id: int,
    owner: str,
    repo_name: str,
    pr_number: int,
    pr: dict[str, Any],
    action: str = "review",
) -> dict[str, Any]:
    """Current GitHub PR JSON -> the dispatch contract, including intent."""
    base = pr.get("base") or {}
    base_repo = base.get("repo") or {}
    return {
        "action": action,
        "installation": {"id": install_id},
        "repository": {
            "id": int(base_repo["id"]),
            "name": repo_name,
            "owner": {"login": owner},
        },
        "pull_request": {
            "number": pr_number,
            "title": str(pr.get("title") or ""),
            "body": str(pr.get("body") or ""),
            "draft": bool(pr.get("draft", False)),
            "user": {
                "login": str((pr.get("user") or {}).get("login") or ""),
            },
            "head": {"sha": (pr.get("head") or {})["sha"]},
            "base": {"sha": base.get("sha")},
        },
    }


def _review_eligible(pr: dict[str, Any]) -> bool:
    """Only open, non-draft pull requests may publish a review."""
    return str(pr.get("state") or "") == "open" and not bool(
        pr.get("draft", False)
    )


def _enqueue_current_review(
    *,
    install_id: int,
    repo_full: str,
    pr_number: int,
    pr: dict[str, Any],
    settle_seconds: int,
) -> None:
    """Durably hand the freshly fetched eligible snapshot back to the lane."""
    enqueue_review(
        install_id=install_id,
        repo=repo_full,
        pr_number=pr_number,
        requested_base_sha=str((pr.get("base") or {}).get("sha") or ""),
        requested_head_sha=str((pr.get("head") or {}).get("sha") or ""),
        requested_title=str(pr.get("title") or ""),
        requested_body=str(pr.get("body") or ""),
        settle_seconds=settle_seconds,
    )


def _review_claim_heartbeat_loop(
    owned_claim_args: dict[str, Any],
    stop: threading.Event,
    ownership_lost: threading.Event,
) -> None:
    from adapters.install_store import renew_review_claim

    while not stop.wait(_REVIEW_CLAIM_HEARTBEAT_SECONDS):
        try:
            renewed = renew_review_claim(
                **owned_claim_args,
                lease_seconds=_REVIEW_CLAIM_LEASE_SECONDS,
            )
        except Exception as error:  # noqa: BLE001 - retry within lease buffer
            log.warning(
                "elder_review_claim_heartbeat_failed",
                extra={
                    "repo": owned_claim_args["repo"],
                    "pr": owned_claim_args["pr_number"],
                    "kind": type(error).__name__,
                },
            )
            continue
        if renewed:
            continue
        ownership_lost.set()
        log.error(
            "elder_review_claim_ownership_lost",
            extra={
                "repo": owned_claim_args["repo"],
                "pr": owned_claim_args["pr_number"],
            },
        )
        return


# --- Active-claim registry (graceful-shutdown release) ----------------------
# A consumer pod that dies mid-review (every deploy rolls it; reviews run
# minutes, the terminationGracePeriod is 30s) orphans its snapshot claim: the
# in-function except/finally never runs, the lease outlives the pod by up to
# _REVIEW_CLAIM_LEASE_SECONDS, and the SQS redelivery bounces off
# "claim busy" - burning receives toward the DLQ (maxReceiveCount) while the
# PR sits without its (now REQUIRED, grug#515) check. The registry tracks
# every in-flight claim so main() can release them all on SIGTERM; the next
# consumer's redelivery then acquires cleanly on its first attempt.
_ACTIVE_REVIEW_CLAIMS: dict[str, dict[str, Any]] = {}
_ACTIVE_REVIEW_CLAIMS_LOCK = threading.Lock()


def _register_active_review_claim(token: str, owned_claim_args: dict[str, Any]) -> None:
    with _ACTIVE_REVIEW_CLAIMS_LOCK:
        _ACTIVE_REVIEW_CLAIMS[token] = owned_claim_args


def _unregister_active_review_claim(token: str) -> None:
    with _ACTIVE_REVIEW_CLAIMS_LOCK:
        _ACTIVE_REVIEW_CLAIMS.pop(token, None)


def release_active_review_claims() -> int:
    """Release every still-registered review claim (graceful shutdown).

    Called by the consumer's main() after its threads were asked to stop: any
    claim still registered belongs to a review that will not finish in this
    process. Releasing lets the SQS redelivery acquire immediately instead of
    bouncing off the orphaned lease. Best-effort per claim - one failed
    release (e.g. ownership already lost to a completing handler racing the
    shutdown) must not stop the rest. Returns the number released."""
    with _ACTIVE_REVIEW_CLAIMS_LOCK:
        claims = list(_ACTIVE_REVIEW_CLAIMS.items())
        _ACTIVE_REVIEW_CLAIMS.clear()
    released = 0
    if not claims:
        return 0
    from adapters.install_store import release_review_claim

    for _token, owned_claim_args in claims:
        try:
            if release_review_claim(**owned_claim_args):
                released += 1
                log.info(
                    "elder_review_claim_released_on_shutdown",
                    extra={
                        "repo": owned_claim_args.get("repo"),
                        "pr": owned_claim_args.get("pr_number"),
                    },
                )
            else:
                log.warning(
                    "elder_review_claim_shutdown_release_lost_ownership",
                    extra={
                        "repo": owned_claim_args.get("repo"),
                        "pr": owned_claim_args.get("pr_number"),
                    },
                )
        except Exception:  # noqa: BLE001 - best-effort during shutdown
            log.warning(
                "elder_review_claim_shutdown_release_failed",
                extra={
                    "repo": owned_claim_args.get("repo"),
                    "pr": owned_claim_args.get("pr_number"),
                },
                exc_info=True,
            )
    return released


def _start_review_claim_heartbeat(
    owned_claim_args: dict[str, Any],
) -> _ReviewClaimHeartbeat:
    stop = threading.Event()
    ownership_lost = threading.Event()
    thread = threading.Thread(
        target=_review_claim_heartbeat_loop,
        args=(owned_claim_args, stop, ownership_lost),
        name="review-claim-heartbeat",
        daemon=True,
    )
    thread.start()
    return _ReviewClaimHeartbeat(
        stop=stop,
        ownership_lost=ownership_lost,
        thread=thread,
    )


def _stop_review_claim_heartbeat(
    heartbeat: _ReviewClaimHeartbeat | None,
) -> bool:
    if heartbeat is None:
        return True
    heartbeat.stop.set()
    heartbeat.thread.join(timeout=1.0)
    return not heartbeat.ownership_lost.is_set()


def _run_hot_review(
    job: dict[str, Any], install_id: int, repo_full: str, pr_number: int,
) -> str:
    """Settle, cancel stale work, then run Elder on one current snapshot.

    The full-snapshot claim happens before the wait so duplicate webhook events
    skip immediately. Base, head, title, or body movement during the quiet
    window cancels this job; the event for the new snapshot owns the next
    durable message.
    """
    owner, sep, repo_name = repo_full.partition("/")
    if not sep or not owner or not repo_name:
        raise ValueError(f"invalid repo coordinate: {repo_full!r}")

    before = _fetch_current_pr(install_id, owner, repo_name, pr_number)
    head_sha = str((before.get("head") or {}).get("sha") or "")
    if not head_sha:
        raise ValueError("current PR has no head SHA")
    if bool(before.get("draft", False)):
        log.info(
            "elder_review_draft_skipped",
            extra={"repo": repo_full, "pr": pr_number, "head_sha": head_sha[:8]},
        )
        return "draft_skipped"
    if str(before.get("state") or "") != "open":
        log.info(
            "elder_review_ineligible_skipped",
            extra={
                "repo": repo_full,
                "pr": pr_number,
                "head_sha": head_sha[:8],
                "state": str(before.get("state") or ""),
            },
        )
        return "pr_ineligible"
    repo_id = int(((before.get("base") or {}).get("repo") or {})["id"])
    snapshot_id = review_snapshot_id_from_pr(before)

    from adapters.install_store import (
        acquire_review_claim,
        complete_review_claim,
        release_review_claim,
    )

    claim_args = {
        "install_id": install_id,
        "repo": repo_full,
        "pr_number": pr_number,
        "persona": "code_reviewer",
        # Legacy store API name; the value is intentionally the canonical
        # review-input identity, while head_sha remains the real commit SHA in
        # dispatch payloads and logs.
        "head_sha": snapshot_id,
    }

    owner_token = uuid.uuid4().hex
    owned_claim_args = {**claim_args, "owner_token": owner_token}
    claim_status = acquire_review_claim(
        **owned_claim_args,
        lease_seconds=_REVIEW_CLAIM_LEASE_SECONDS,
    )
    if claim_status == "completed":
        log.info(
            "elder_review_duplicate_snapshot_skipped",
            extra={
                "repo": repo_full,
                "pr": pr_number,
                "head_sha": head_sha[:8],
                "snapshot_id": snapshot_id[:11],
            },
        )
        return "duplicate_snapshot"
    if claim_status != "acquired":
        log.info(
            "elder_review_snapshot_claim_busy",
            extra={
                "repo": repo_full,
                "pr": pr_number,
                "head_sha": head_sha[:8],
                "snapshot_id": snapshot_id[:11],
            },
        )
        raise RuntimeError("Elder review snapshot claim is still in progress")

    heartbeat: _ReviewClaimHeartbeat | None = None
    # Register for graceful-shutdown release; the finally-unregister runs on
    # every in-process exit (normal, except-release, raise), so the shutdown
    # sweep only ever sees claims whose handler was killed mid-flight.
    _register_active_review_claim(owner_token, owned_claim_args)
    try:
        heartbeat = _start_review_claim_heartbeat(owned_claim_args)
        settle_seconds = min(
            _MAX_SETTLE_SECONDS,
            max(0, int(job.get("settle_seconds", 0))),
        )
        if settle_seconds:
            log.info(
                "elder_review_settling",
                extra={
                    "repo": repo_full,
                    "pr": pr_number,
                    "head_sha": head_sha[:8],
                    "snapshot_id": snapshot_id[:11],
                    "settle_seconds": settle_seconds,
                },
            )
            time.sleep(settle_seconds)

        after = _fetch_current_pr(install_id, owner, repo_name, pr_number)
        current_head = str((after.get("head") or {}).get("sha") or "")
        current_snapshot_id = review_snapshot_id_from_pr(after)
        if not _review_eligible(after):
            if not _stop_review_claim_heartbeat(heartbeat):
                raise RuntimeError(
                    "Elder review claim ownership lost during settle"
                )
            if not release_review_claim(**owned_claim_args):
                raise RuntimeError(
                    "Elder review claim ownership lost for ineligible PR"
                )
            log.info(
                "elder_review_ineligible_after_settle",
                extra={
                    "repo": repo_full,
                    "pr": pr_number,
                    "head_sha": current_head[:8],
                    "state": str(after.get("state") or ""),
                    "draft": bool(after.get("draft", False)),
                },
            )
            return "pr_ineligible"
        if current_snapshot_id != snapshot_id:
            _enqueue_current_review(
                install_id=install_id,
                repo_full=repo_full,
                pr_number=pr_number,
                pr=after,
                settle_seconds=settle_seconds,
            )
            if not _stop_review_claim_heartbeat(heartbeat):
                raise RuntimeError(
                    "Elder review claim ownership lost for stale snapshot"
                )
            if not release_review_claim(**owned_claim_args):
                raise RuntimeError(
                    "Elder review claim ownership lost while cancelling stale snapshot"
                )
            log.info(
                "elder_review_stale_snapshot_cancelled",
                extra={
                    "repo": repo_full,
                    "pr": pr_number,
                    "claimed_head_sha": head_sha[:8],
                    "current_head_sha": current_head[:8],
                    "claimed_snapshot_id": snapshot_id[:11],
                    "current_snapshot_id": current_snapshot_id[:11],
                },
            )
            return "stale_snapshot"

        cfg = get_repo_config(install_id, repo_id)
        result = dispatch_code_review(
            _review_payload(
                install_id=install_id,
                owner=owner,
                repo_name=repo_name,
                pr_number=pr_number,
                pr=after,
            ),
            blocking=bool(cfg.get("code_reviewer_blocking", False)),
        )
        degraded_reason = result.get("degraded_reason", "")
        if degraded_reason == "stale_snapshot":
            latest = _fetch_current_pr(
                install_id, owner, repo_name, pr_number,
            )
            if _review_eligible(latest):
                _enqueue_current_review(
                    install_id=install_id,
                    repo_full=repo_full,
                    pr_number=pr_number,
                    pr=latest,
                    settle_seconds=settle_seconds,
                )
            if not _stop_review_claim_heartbeat(heartbeat):
                raise RuntimeError(
                    "Elder review claim ownership lost during dispatch"
                )
            if not release_review_claim(**owned_claim_args):
                raise RuntimeError(
                    "Elder review claim ownership lost after stale dispatch"
                )
            return (
                "stale_snapshot"
                if _review_eligible(latest)
                else "pr_ineligible"
            )
        if degraded_reason == "pr_ineligible":
            if not _stop_review_claim_heartbeat(heartbeat):
                raise RuntimeError(
                    "Elder review claim ownership lost during dispatch"
                )
            if not release_review_claim(**owned_claim_args):
                raise RuntimeError(
                    "Elder review claim ownership lost for ineligible dispatch"
                )
            return "pr_ineligible"
        result_status = result.get("result")
        if result_status == "publish_failed":
            raise RuntimeError("Elder review publication failed")
        if result_status == "skipped" and degraded_reason != "no_diff":
            raise RuntimeError(
                f"Elder review degraded: {degraded_reason or 'unknown'}"
            )
        if result_status not in {"pass", "fail", "skipped"}:
            raise RuntimeError(
                f"Elder review returned unexpected result: {result_status!r}"
            )
        if not _stop_review_claim_heartbeat(heartbeat):
            raise RuntimeError("Elder review claim ownership lost during review")
        if not complete_review_claim(**owned_claim_args):
            raise RuntimeError("Elder review claim completion lost ownership")
    except Exception:
        _stop_review_claim_heartbeat(heartbeat)
        try:
            released = release_review_claim(**owned_claim_args)
            if not released:
                raise RuntimeError("Elder review claim release lost ownership")
        except Exception as release_error:  # noqa: BLE001 - preserve primary failure
            log.error(
                "elder_review_claim_release_failed",
                extra={
                    "repo": repo_full,
                    "pr": pr_number,
                    "head_sha": head_sha[:8],
                    "snapshot_id": snapshot_id[:11],
                    "kind": type(release_error).__name__,
                },
                exc_info=True,
            )
        raise
    finally:
        _unregister_active_review_claim(owner_token)
    log.info(
        "elder_review_durable_done",
        extra={"repo": repo_full, "pr": pr_number, **result},
    )
    return "dispatched"


def handle_rerun_jobs(event: dict[str, Any]) -> dict[str, int]:
    """Consume `grug-rerun-jobs` SQS records (event-source mapping, batch 1).

    Unlike the cave result handler, a failed job is allowed to RAISE so the ESM
    retries it (visibility timeout) → DLQ after `maxReceiveCount`. With batch
    size 1 each invocation owns exactly one message, so a raise re-drives only
    that job. Returns a summary for the structured log on the success path."""
    records = event.get("Records", []) if isinstance(event, dict) else []
    statuses: list[str] = []
    for rec in records:
        body = rec.get("body", "") if isinstance(rec, dict) else ""
        statuses.append(_run_one(body))  # may raise → ESM retry → DLQ
    return {
        "records": len(records),
        "dispatched": statuses.count("dispatched"),
        "skipped": sum(
            1 for status in statuses
            if status in {
                "skipped_persona", "duplicate_snapshot", "stale_snapshot",
                "draft_skipped", "pr_ineligible",
            }
        ),
    }
