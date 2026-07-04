# WEBHOOK-ONLY (NOT mirrored): Elder async-offload + its async-job routing.
# The api service has no webhook ACK path, so there is no api sibling — like
# dispatcher.py / main.py / consumer.py. Per ADR-0001, only modules BOTH
# services run are mirrored.
"""Async offload of the Elder LLM review off the webhook ACK path (#272).

`receive_github_webhook` must ACK GitHub in <10s, but the Elder persona
makes 1–2 LLM calls (worst case ~300s with retries) — far over GitHub's
~10s delivery timeout. So the review is run OFF the ACK path: the handler
enqueues it and returns immediately.

On the k8s runtime (#354/#368) the offload is an in-process background
thread (`_spawn_local_elder`): the webhook pod already carries every
dependency the Elder path needs (GitHub-App auth, LLM keys, Postgres,
LLM-Obs, KMS), so a separate worker would duplicate ~15 env vars + secret
grants + a role + the mirror surface ADR-0001 exists to prevent. The
thread runs with the pod's full lifetime; a pod restart mid-review drops
the in-flight review, the same best-effort contract Lambda's async invoke
had (a dropped review re-triggers on the next push). See `specs/DESIGN.md`
→ "Async Elder offload (#272)".

Routing: an async job is tagged with the `grug_async_job` sentinel so a
router can dispatch raw async events to `run_elder_job`.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.async_dispatch")

# Sentinel marking an async job. Routing keys on `event.get("grug_async_job")`
# truthiness; the value names the job kind so job types can fan out from one
# router. Guard (#466) is the second async job type the sentinel anticipated.
ASYNC_JOB_KEY = "grug_async_job"
ELDER_REVIEW_JOB = "elder_review"
GUARD_REVIEW_JOB = "guard_review"


def _slim_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the GitHub PR payload to ONLY the fields the Elder worker
    (`dispatch_code_review`) reads: `action`, the PR number + head sha,
    the repo owner/name, and the installation id. The worker re-fetches
    the diff from GitHub by those IDs, so forwarding the full payload is
    unnecessary: the slim projection keeps the job minimal and bounded
    regardless of PR size (a long body + two full repo objects + sender/org
    are all dropped). Mirrors `dispatch_code_review`'s reads — keep in sync
    if that function starts consuming new payload fields.
    """
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    return {
        "action": payload.get("action", ""),
        "pull_request": {
            "number": pr.get("number"),
            "head": {"sha": (pr.get("head") or {}).get("sha")},
        },
        "repository": {
            "owner": {"login": (repo.get("owner") or {}).get("login")},
            "name": repo.get("name"),
        },
        "installation": {"id": (payload.get("installation") or {}).get("id")},
    }


def enqueue_elder_review(
    *,
    payload: dict[str, Any],
    delivery_id: str,
    blocking: bool,
) -> bool:
    """Fire-and-forget offload to run the Elder review async.

    Returns ``True`` if the async job was accepted, ``False`` on
    any failure (logged). Best-effort by design: the caller logs the
    failure and returns ``result="enqueue_failed"`` — it does NOT fall
    back to a synchronous Elder run, because that would re-block the ACK
    path and break the <10s guarantee. A dropped review re-triggers on
    the next push.

    The k8s runtime (``GRUG_K8S_RUNTIME`` set in the pod manifests, #368)
    runs the job in-process on a background thread: the ACK handler returns
    immediately while the thread runs with the pod's full lifetime. This is
    the only async path post-Lambda (#354); local / test (the flag unset)
    has no offload and returns False.

    k8s trade-off (vs a queue + worker Deployment, recorded in
    specs/DESIGN.md): a pod restart mid-review drops the in-flight
    review — a best-effort contract (a dropped review re-triggers on the
    next push). In exchange we avoid a new queue + consumer + IAM surface
    for the hot path.
    """
    job = {
        ASYNC_JOB_KEY: ELDER_REVIEW_JOB,
        "delivery_id": delivery_id,
        "blocking": blocking,
        # Slim projection — NOT the full payload. See _slim_payload.
        "payload": _slim_payload(payload),
    }
    if os.getenv("GRUG_K8S_RUNTIME"):
        return _spawn_local_elder(job)
    log.warning("elder_enqueue_no_runtime", extra={"delivery_id": delivery_id})
    return False


def _spawn_local_elder(job: dict[str, Any]) -> bool:
    """Run the Elder job on a daemon thread (k8s runtime, #368).

    daemon=True is deliberate: on pod shutdown (deploy rollout, node
    drain) an in-flight review dies WITHOUT blocking termination —
    matching the documented best-effort contract. `run_elder_job` owns
    idempotency + never-raise, so the thread body needs no wrapper.
    """
    delivery_id = str(job.get("delivery_id", ""))
    try:
        threading.Thread(
            target=run_elder_job,
            args=(job,),
            name=f"elder-{delivery_id[:13]}",
            daemon=True,
        ).start()
        return True
    except Exception as e:  # noqa: BLE001 — best-effort enqueue; never break the ACK
        # Same monitor contract as the Lambda branch: one error line per
        # dropped review, with the cause kind.
        log.error(
            "elder_enqueue_invoke_error",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
        )
        return False


def enqueue_guard_review(
    *,
    payload: dict[str, Any],
    delivery_id: str,
    blocking: bool,
) -> bool:
    """Fire-and-forget offload for the Guard security review (#466) -
    same contract, runtime, and trade-offs as `enqueue_elder_review`
    (see its docstring): background daemon thread under GRUG_K8S_RUNTIME,
    False on any failure, NO sync fallback, a dropped scan re-triggers on
    the next push. Kept as its own function (not a persona-generic one)
    deliberately: Elder's enqueue is a live patch target and its monitored
    log names must stay byte-identical; a THIRD async persona triggers the
    rule-of-three generalization (ADR-0012)."""
    job = {
        ASYNC_JOB_KEY: GUARD_REVIEW_JOB,
        "delivery_id": delivery_id,
        "blocking": blocking,
        "payload": _slim_payload(payload),
    }
    if os.getenv("GRUG_K8S_RUNTIME"):
        delivery = str(job.get("delivery_id", ""))
        try:
            threading.Thread(
                target=run_guard_job,
                args=(job,),
                name=f"guard-{delivery[:13]}",
                daemon=True,
            ).start()
            return True
        except Exception as e:  # noqa: BLE001 — best-effort enqueue; never break the ACK
            log.error(
                "guard_enqueue_invoke_error",
                extra={"delivery_id": delivery, "kind": type(e).__name__},
            )
            return False
    log.warning("guard_enqueue_no_runtime", extra={"delivery_id": delivery_id})
    return False


def run_guard_job(event: dict[str, Any]) -> dict[str, str]:
    """Worker entry for an async Guard security review (#466). Same
    two-layer idempotency + never-raise contract as `run_elder_job` (see
    its docstring), with two deliberate differences:

    - The delivery claim key is NAMESPACED (`{delivery_id}:guard`):
      Elder and Guard both dispatch from the SAME webhook delivery, and
      `claim_delivery` is keyed on the raw GUID - an unnamespaced claim
      would let whichever persona ran first mark the delivery consumed
      and silently skip the other.
    - No #418 self-recover: the rerun lane's persona surface doesn't
      carry guard yet; a dropped scan re-triggers on the next push
      (tracer contract, ADR-0012).
    """
    delivery_id = str(event.get("delivery_id", ""))
    try:
        from adapters.install_store import claim_delivery

        if not claim_delivery(f"{delivery_id}:guard"):
            log.info("guard_job_duplicate_skipped", extra={"delivery_id": delivery_id})
            return {"status": "skipped", "reason": "duplicate_delivery"}
    except Exception as e:  # noqa: BLE001 — claim is best-effort; degrade to running
        log.warning(
            "guard_job_claim_failed_running_anyway",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
        )

    payload = event.get("payload") or {}
    try:
        from adapters.install_store import claim_review

        install_id, repo, pr_number = _pr_ids(payload)
        head_sha = ((payload.get("pull_request") or {}).get("head") or {}).get("sha")
        if install_id and repo and pr_number and head_sha:
            if not claim_review(
                install_id=install_id,
                repo=repo,
                pr_number=pr_number,
                persona="guard",
                head_sha=head_sha,
            ):
                log.info(
                    "guard_job_duplicate_sha_skipped",
                    extra={
                        "delivery_id": delivery_id, "repo": repo,
                        "pr": pr_number, "head_sha": head_sha,
                    },
                )
                return {"status": "skipped", "reason": "duplicate_head_sha"}
    except Exception as e:  # noqa: BLE001 — claim is best-effort; degrade to running
        log.warning(
            "guard_job_review_claim_failed_running_anyway",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
        )

    blocking = bool(event.get("blocking", False))
    try:
        from personas.guard.dispatch import dispatch_guard_review

        result = dispatch_guard_review(payload, blocking=blocking)
        log.info(
            "guard_job_done",
            extra={"delivery_id": delivery_id, **result},
        )
        return result
    except Exception as e:  # noqa: BLE001 — never retry-storm; degrade contract owns this
        log.error(
            "guard_job_unhandled",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
            exc_info=True,
        )
        return {"persona": "guard", "result": "unhandled_error"}


def run_elder_job(event: dict[str, Any]) -> dict[str, str]:
    """Worker entry for an async Elder review job.

    Two-layer idempotency so the review is never double-posted:
    ``delivery_id`` (`install_store.claim_delivery`) skips a GitHub
    redelivery / async-invoke retry of the SAME delivery, and the EXACT
    head SHA (`install_store.claim_review`, #397) skips a same-SHA
    re-trigger across DIFFERENT deliveries - a non-push event (`edited`
    on the PR body, `ready_for_review`) that carries an already-reviewed
    head SHA. Every NEW head SHA still wins a fresh review.

    NEVER re-raises: an unhandled error here would make AWS retry the
    async invocation (a retry storm), and we already have our own
    idempotency + the advisory-degrade contract inside
    `dispatch_code_review`. All failures are logged and returned as a
    status dict instead.
    """
    delivery_id = str(event.get("delivery_id", ""))
    try:
        # Lazy import INSIDE the guard (#272): keeps the SYNC (HTTP) cold-start
        # cheap (the Elder dep graph only loads in the async invocation), AND
        # — load-bearing — an import failure here must NOT escape and trigger
        # AWS's 2 default async retries (the retry-storm this function exists
        # to prevent). A failed import degrades to running, same as a DDB
        # hiccup on the claim.
        from adapters.install_store import claim_delivery

        if not claim_delivery(delivery_id):
            log.info("elder_job_duplicate_skipped", extra={"delivery_id": delivery_id})
            return {"status": "skipped", "reason": "duplicate_delivery"}
    except Exception as e:  # noqa: BLE001 — claim is best-effort; degrade to running
        # A DDB hiccup on the claim must not drop the review. Fail OPEN
        # (run it): a possible duplicate beats a silently-skipped review.
        log.warning(
            "elder_job_claim_failed_running_anyway",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
        )

    payload = event.get("payload") or {}

    # Per-head-SHA idempotency (#397): claim_delivery (above) only catches an
    # exact-delivery retry; this catches a same-head-SHA re-trigger across
    # DIFFERENT deliveries (a redelivery, or a non-push `edited` /
    # `ready_for_review` event) so unchanged code is not re-reviewed - while
    # every NEW head SHA still wins a fresh review. Best-effort: a missing id
    # / head SHA, or a claim DB error, falls through to running the review
    # (fail OPEN - a possible double-review beats a silent skip).
    try:
        from adapters.install_store import claim_review

        install_id, repo, pr_number = _pr_ids(payload)
        head_sha = ((payload.get("pull_request") or {}).get("head") or {}).get("sha")
        if install_id and repo and pr_number and head_sha:
            if not claim_review(
                install_id=install_id,
                repo=repo,
                pr_number=pr_number,
                persona="code_reviewer",
                head_sha=head_sha,
            ):
                log.info(
                    "elder_job_duplicate_sha_skipped",
                    extra={
                        "delivery_id": delivery_id, "repo": repo,
                        "pr": pr_number, "head_sha": head_sha,
                    },
                )
                return {"status": "skipped", "reason": "duplicate_head_sha"}
    except Exception as e:  # noqa: BLE001 — claim is best-effort; degrade to running
        log.warning(
            "elder_job_review_claim_failed_running_anyway",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
        )

    blocking = bool(event.get("blocking", False))
    try:
        from personas.code_reviewer.dispatch import dispatch_code_review

        result = dispatch_code_review(payload, blocking=blocking)
        log.info(
            "elder_job_done",
            extra={"delivery_id": delivery_id, **result},
        )
        return result
    except Exception as e:  # noqa: BLE001 — never retry-storm; degrade contract owns this
        log.error(
            "elder_job_unhandled",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
            exc_info=True,
        )
        # Self-recover (#418): a dropped review used to wait for a human to
        # re-push. Enqueue ONE durable re-run to grug-rerun-jobs instead - the
        # consumer re-runs it with the SQS redrive contract (visibility timeout
        # -> DLQ after maxReceiveCount), so a transient failure heals on its own
        # and a persistent one lands in the DLQ as a terminal, visible signal.
        _self_recover_review(payload, delivery_id)
        return {"persona": "code_reviewer", "result": "unhandled_error"}


def _pr_ids(payload: dict[str, Any]) -> tuple[int | None, str | None, int | None]:
    """Extract (install_id, "owner/name", pr_number) from the slim payload for a
    self-recovery re-run. Any missing -> None (caller skips)."""
    inst = (payload.get("installation") or {}).get("id")
    repo = payload.get("repository") or {}
    owner = (repo.get("owner") or {}).get("login")
    name = repo.get("name")
    pr_number = (payload.get("pull_request") or {}).get("number")
    full = f"{owner}/{name}" if owner and name else None
    return (
        int(inst) if inst is not None else None,
        full,
        int(pr_number) if pr_number is not None else None,
    )


def _self_recover_review(payload: dict[str, Any], delivery_id: str) -> None:
    """Enqueue ONE durable re-run for a dropped Elder review (#418). Bounded:
    enqueues at most once per drop - the rerun CONSUMER retries via SQS redrive,
    never re-enqueues (it calls dispatch_code_review directly, not run_elder_job),
    so there is no loop. Best-effort: a failure to enqueue is logged, never
    raised (the caller is already in its degrade path)."""
    try:
        install_id, repo, pr_number = _pr_ids(payload)
        if not (install_id and repo and pr_number):
            log.warning(
                "elder_self_recover_skipped_no_ids", extra={"delivery_id": delivery_id}
            )
            return
        from rerun import enqueue_rerun

        enqueue_rerun(
            install_id=install_id, repo=repo, pr_number=pr_number, persona="elder"
        )
        log.info(
            "elder_self_recover_enqueued",
            extra={"delivery_id": delivery_id, "repo": repo, "pr": pr_number},
        )
    except Exception as e:  # noqa: BLE001 — recovery is best-effort, never raises
        # exc_info for symmetry with elder_job_unhandled: if recovery is
        # systematically broken (queue misconfig), the stack speeds triage.
        log.error(
            "elder_self_recover_failed",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
            exc_info=True,
        )
