"""Smasher persona - webhook pull_request dispatch (#469, ADR-0010/0013).

The registry loop resolves this module from the PersonaSpec's
`dispatch_module` and calls `dispatch_pull_request(ctx)`. Smasher launches a
locked-down k8s Job that runs the repo's tests per mutant (far over the ACK
budget), so like Elder/Guard it is ENQUEUED off the ACK path and returns
immediately; `async_dispatch.run_smasher_job` executes it. Enqueue failure does
NOT fall back to a sync run; a dropped Trial re-triggers on the next push.

`async_dispatch` is webhook-only, so the imports stay inside the function
(this shared module is also importable by the api service, where the loop
never runs - ADR-0014).
"""

from __future__ import annotations

import logging
import os

from personas.registry import PullRequestContext

log = logging.getLogger(
    f"{os.getenv('DD_SERVICE', 'grug')}.persona.smasher.webhook_dispatch"
)


def dispatch_pull_request(ctx: PullRequestContext) -> dict[str, str]:
    """Enqueue the Smasher Trial. `ctx.blocking` is always False (Smasher has no
    blocking flag - mutation findings are advisory)."""
    from async_dispatch import enqueue_smasher_review  # lazy: webhook-only + cold-start

    enqueued = enqueue_smasher_review(
        payload=ctx.payload,
        delivery_id=ctx.delivery_id,
        blocking=ctx.blocking,
    )
    if not enqueued:
        # Same #478 resolution as Guard: an enqueue failure (missing runtime
        # flag / thread-spawn error) enqueues one durable re-run on the SQS
        # rerun lane, consumed by grug-consumer which survives the pod-local
        # breakage that made this enqueue fail.
        from async_dispatch import self_recover_review  # lazy: webhook-only

        self_recover_review(ctx.payload, ctx.delivery_id, persona="smasher")
        log.error(
            "smasher_enqueue_failed",
            extra={
                "installation_id": ctx.installation_id,
                "owner": ctx.owner,
                "repo": ctx.repo_name,
                "pr_number": ctx.pr_number,
                "delivery_id": ctx.delivery_id,
            },
        )
    return {
        "persona": "smasher",
        "result": "queued" if enqueued else "enqueue_failed",
    }
