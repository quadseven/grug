"""Guard persona - webhook pull_request dispatch (#466, ADR-0010/0012).

The registry loop resolves this module from the PersonaSpec's
`dispatch_module` and calls `dispatch_pull_request(ctx)`. Guard runs the
security suite (LLM exploitability judge inside), so like Elder it is
ENQUEUED off the ACK path and returns immediately; `async_dispatch.
run_guard_job` executes it. Enqueue failure does NOT fall back to a sync
run; a dropped scan re-triggers on the next push.

`async_dispatch` is webhook-only, so the import stays inside the
function (this shared module is also importable by the api service, where the loop
never runs).
"""

from __future__ import annotations

import logging
import os

from personas.registry import PullRequestContext

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.guard.webhook_dispatch")


def dispatch_pull_request(ctx: PullRequestContext) -> dict[str, str]:
    """Enqueue the Guard security review. `ctx.blocking` carries the
    repo's `guard_blocking` flag (defaults False - advisory-first)."""
    from async_dispatch import enqueue_guard_review  # lazy: webhook-only + cold-start

    enqueued = enqueue_guard_review(
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

        self_recover_review(ctx.payload, ctx.delivery_id, persona="guard")
        log.error(
            "guard_enqueue_failed",
            extra={
                "installation_id": ctx.installation_id,
                "owner": ctx.owner,
                "repo": ctx.repo_name,
                "pr_number": ctx.pr_number,
                "delivery_id": ctx.delivery_id,
            },
        )
    return {
        "persona": "guard",
        "result": "queued" if enqueued else "enqueue_failed",
    }
