# WEBHOOK-ONLY (NOT mirrored): self-invoke of the grug-webhook Lambda + its
# async-job routing. The api service has no webhook ACK path, so there is no
# api sibling — like dispatcher.py / main.py / lambda_handler.py. Per ADR-0001,
# only modules BOTH services run are mirrored.
"""Async offload of the Elder LLM review off the webhook ACK path (#272).

`receive_github_webhook` must ACK GitHub in <10s, but the Elder persona
makes 1–2 LLM calls (worst case ~300s with retries) — far over GitHub's
~10s delivery timeout. So the webhook **self-invokes** the same Lambda
asynchronously (`InvocationType="Event"`) and returns immediately; the
async invocation runs the Elder dispatch with the full Lambda budget.

Why self-invoke the SAME function rather than a dedicated worker Lambda
or SQS: the webhook image already carries every dependency the Elder
path needs (GitHub-App auth, LLM keys, DDB, LLM-Obs, KMS) — a separate
worker would duplicate ~15 env vars + secret grants + a role + the
mirror surface (the drift ADR-0001 exists to prevent). A same-function
async invocation IS the async worker. See `specs/DESIGN.md` →
"Async Elder offload (#272)".

Routing: the async invocation arrives at `lambda_handler.handler` as a
RAW event (not a Function-URL HTTP event), so the handler sniffs the
`grug_async_job` sentinel and calls `run_elder_job` BEFORE handing the
event to Mangum (which would choke on a non-HTTP shape).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.async_dispatch")

# Sentinel marking a self-invoked async job. `lambda_handler.handler`
# routes on `event.get("grug_async_job")` truthiness; the value names the
# job kind so a future second async job type can fan out from one router.
ASYNC_JOB_KEY = "grug_async_job"
ELDER_REVIEW_JOB = "elder_review"

_lambda = boto3.client("lambda")


def enqueue_elder_review(
    *, payload: dict[str, Any], delivery_id: str, blocking: bool,
) -> bool:
    """Fire-and-forget self-invoke to run the Elder review async.

    Returns ``True`` if the async invocation was accepted, ``False`` on
    any failure (logged). Best-effort by design: the caller logs the
    failure and returns ``result="enqueue_failed"`` — it does NOT fall
    back to a synchronous Elder run, because that would re-block the ACK
    path and break the <10s guarantee. A dropped review re-triggers on
    the next push.

    The target function is the caller's OWN function, read from the
    Lambda-runtime-provided ``AWS_LAMBDA_FUNCTION_NAME``; missing (local
    / test) → we can't self-invoke, return False.
    """
    function_name = os.getenv("AWS_LAMBDA_FUNCTION_NAME", "")
    if not function_name:
        log.warning("elder_enqueue_no_function_name", extra={"delivery_id": delivery_id})
        return False
    job = {
        ASYNC_JOB_KEY: ELDER_REVIEW_JOB,
        "delivery_id": delivery_id,
        "blocking": blocking,
        "payload": payload,
    }
    try:
        _lambda.invoke(
            FunctionName=function_name,
            InvocationType="Event",  # async; returns 202, does not wait
            Payload=json.dumps(job).encode("utf-8"),
        )
        return True
    except Exception as e:  # noqa: BLE001 — best-effort enqueue; never break the ACK
        log.error(
            "elder_enqueue_failed",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
        )
        return False


def run_elder_job(event: dict[str, Any]) -> dict[str, str]:
    """Worker entry for a self-invoked Elder review job.

    Idempotent on ``delivery_id`` (`install_store.claim_delivery`): a
    GitHub redelivery or an AWS async-invoke retry of the SAME delivery
    claims-and-skips, so the review is never double-posted.

    NEVER re-raises: an unhandled error here would make AWS retry the
    async invocation (a retry storm), and we already have our own
    idempotency + the advisory-degrade contract inside
    `dispatch_code_review`. All failures are logged and returned as a
    status dict instead.
    """
    delivery_id = str(event.get("delivery_id", ""))
    # Lazy imports keep the cold-start of the SYNC (HTTP) path cheap — the
    # Elder dependency graph (LLM client, diff parser) only loads in the
    # async invocation that actually needs it.
    from adapters.install_store import claim_delivery

    try:
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
        return {"persona": "code_reviewer", "result": "unhandled_error"}
