"""Elder (code-reviewer) persona - webhook pull_request dispatch (ADR-0010, #465).

Elder's quiet window and two independent review calls run far beyond GitHub's
delivery timeout, so this entry enqueues the review off the ACK path and
returns `queued` immediately. Production uses the durable rerun FIFO; the
consumer owns settling, snapshot-scoped idempotency, stale cancellation, and
redrive. A durable queue failure escapes this async handoff so the webhook
delivery is recorded failed and the replay poller can redeliver it; GitHub does
not retry automatically. The local thread fallback never runs synchronously
and retains the existing best-effort self-recovery path.

`async_dispatch` is a webhook-only module, so the import stays inside the
function: this shared module (services/_shared/, ADR-0014) is also
importable by the api service, where the dispatch loop never runs - the
lazy import keeps it import-safe there. It also preserves the historical
patch target (`async_dispatch.enqueue_elder_review`).
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
        # Local/backward-compatible thread handoff only. Production durable
        # queue errors raise above and are recorded as failed webhook
        # deliveries for the replay poller by the registry boundary.
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
