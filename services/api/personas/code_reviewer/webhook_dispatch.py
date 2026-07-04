# MIRRORED — sibling at services/webhook/personas/code_reviewer/webhook_dispatch.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Elder (code-reviewer) persona - webhook pull_request dispatch (ADR-0010, #465).

Elder makes 1-2 LLM calls (worst case ~300s) - far over GitHub's ~10s
delivery timeout - so this entry ENQUEUES the review off the ACK path
(#272) and returns `queued` immediately; `async_dispatch.run_elder_job`
executes it with its own final guard. Enqueue failure does NOT fall back
to a synchronous run - that would re-block the ACK; a dropped review
re-triggers on the next push. Idempotency keys on `ctx.delivery_id`
inside the worker.

`async_dispatch` is a webhook-only module, so the import stays inside the
function: this file is mirrored into the api service (ADR-0001) where the
dispatch loop never runs, and the lazy import keeps the api copy
import-safe. It also preserves the historical patch target
(`async_dispatch.enqueue_elder_review`).
"""

from __future__ import annotations

import logging
import os

from personas.registry import PullRequestContext

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.webhook_dispatch")


def dispatch_pull_request(ctx: PullRequestContext) -> dict[str, str]:
    """Enqueue the Elder review. `ctx.blocking` carries the repo's
    `code_reviewer_blocking` flag (defaults False; operator flips via
    dashboard once LLM-finding trust is established - advisory-first
    prevents false positives from blocking velocity)."""
    from async_dispatch import enqueue_elder_review  # lazy: webhook-only + keeps cold-start cheap

    enqueued = enqueue_elder_review(
        payload=ctx.payload,
        delivery_id=ctx.delivery_id,
        blocking=ctx.blocking,
    )
    if not enqueued:
        # #478 resolution: an enqueue failure (missing runtime flag /
        # thread-spawn error) now ALSO enqueues one durable re-run on the
        # SQS rerun lane - consumed by the separate grug-consumer
        # deployment, which survives exactly the pod-local breakage that
        # made this enqueue fail. Chosen over a 503 (GitHub redelivery
        # storms + webhook auto-disable on persistent misconfig) and over
        # the old silent drop-and-monitor. The error log below stays -
        # it drives the offload monitor.
        from async_dispatch import self_recover_review  # lazy: webhook-only

        self_recover_review(ctx.payload, ctx.delivery_id, persona="elder")
        log.error(
            "elder_enqueue_failed",
            extra={
                "installation_id": ctx.installation_id,
                "owner": ctx.owner,
                "repo": ctx.repo_name,
                "pr_number": ctx.pr_number,
                "delivery_id": ctx.delivery_id,
            },
        )
    return {
        "persona": "code_reviewer",
        "result": "queued" if enqueued else "enqueue_failed",
    }
