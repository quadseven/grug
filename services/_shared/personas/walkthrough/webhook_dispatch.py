"""Teller persona - webhook pull_request dispatch (#554, ADR-0010).

The registry loop resolves this module from the PersonaSpec's
`dispatch_module` and calls `dispatch_pull_request(ctx)`. Teller's
summary call + upsert-comment I/O runs far over the webhook ACK budget,
so like Guard it is ENQUEUED off the ACK path and returns immediately;
`async_dispatch.run_walkthrough_job` executes it. Enqueue failure does
NOT fall back to a sync run; a dropped walkthrough re-triggers on the
next push (the same best-effort contract every async persona shares).

`async_dispatch` is webhook-only, so the import stays inside the
function (this shared module is also importable by the api service,
where the loop never runs).

Walkthrough is opt-in per PR: it only runs when the PR has the
`grug:walkthrough` label, which can be added via the `/grug walkthrough`
comment command.
"""

from __future__ import annotations

import logging
import os

from personas.registry import PullRequestContext

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.walkthrough.webhook_dispatch")

_OPT_IN_LABEL = "grug:walkthrough"


def _has_opt_in_label(payload: dict[str, Any]) -> bool:
    """Check if the PR has the walkthrough opt-in label."""
    pr = payload.get("pull_request") or {}
    labels = pr.get("labels") or []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else getattr(label, "name", "")
        if name == _OPT_IN_LABEL:
            return True
    return False


def dispatch_pull_request(ctx: PullRequestContext) -> dict[str, str]:
    """Enqueue the Teller walkthrough. `ctx.blocking` is always False
    (Teller has no `blocking_flag`) - carried through only to match the
    shared enqueue signature.

    Walkthrough only runs if the PR has the opt-in label. This makes
    Teller an opt-in persona per PR rather than running on every PR.
    """
    if not _has_opt_in_label(ctx.payload):
        log.info(
            "walkthrough_opt_in_missing",
            extra={
                "installation_id": ctx.installation_id,
                "owner": ctx.owner,
                "repo": ctx.repo_name,
                "pr_number": ctx.pr_number,
                "delivery_id": ctx.delivery_id,
            },
        )
        return {"persona": "walkthrough", "result": "opt_in_required"}

    from async_dispatch import enqueue_walkthrough_review  # lazy: webhook-only + cold-start

    enqueued = enqueue_walkthrough_review(
        payload=ctx.payload,
        delivery_id=ctx.delivery_id,
        blocking=ctx.blocking,
    )
    if not enqueued:
        from async_dispatch import self_recover_review  # lazy: webhook-only

        self_recover_review(ctx.payload, ctx.delivery_id, persona="walkthrough")
        log.error(
            "walkthrough_enqueue_failed",
            extra={
                "installation_id": ctx.installation_id,
                "owner": ctx.owner,
                "repo": ctx.repo_name,
                "pr_number": ctx.pr_number,
                "delivery_id": ctx.delivery_id,
            },
        )
    return {
        "persona": "walkthrough",
        "result": "queued" if enqueued else "enqueue_failed",
    }
