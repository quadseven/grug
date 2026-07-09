"""Chief (TPM) persona - webhook pull_request dispatch (ADR-0010, #465).

The registry dispatch loop resolves this module from the PersonaSpec's
`dispatch_module` string and calls `dispatch_pull_request(ctx)`. The body
moved verbatim from `dispatcher._dispatch_tpm`: pure evaluate + publish,
catching BOTH evaluate-time and publish-time exceptions locally so the
personas dispatched after this one still run (independence, #185).

Imports stay inside the function: the ACK path pays import cost only for
personas that actually dispatch, and the historical patch targets
(`personas.tpm.persona.evaluate_pull_request` / `publish_tpm_evaluation`)
keep intercepting.

Since #550, `publish_tpm_evaluation` publishes via the shared
`publish_persona_check` seam and returns the seam's result map instead of
raising on a failed publish — so the httpx-shaped catch that used to live
here is gone (the seam classifies every publish failure into the returned
`"publish_failed"` sentinel and logs `tpm_publish_failed` itself), and the
persona result is read straight off the returned map.
"""
from __future__ import annotations

import logging
import os

from personas.registry import PullRequestContext

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.tpm.webhook_dispatch")


def dispatch_pull_request(ctx: PullRequestContext) -> dict[str, str]:
    """TPM persona dispatch - runs inline (fast, no LLM). Catches its own
    exceptions so a future bug in `evaluate_pull_request` or an import-time
    failure of personas.tpm cannot propagate and starve the other personas.
    """
    try:
        from personas.tpm.persona import (  # type: ignore
            evaluate_pull_request, publish_tpm_evaluation,
        )
        evaluation = evaluate_pull_request(ctx.pr_body)
        result_map = publish_tpm_evaluation(
            evaluation,
            installation_id=ctx.installation_id,
            owner=ctx.owner,
            repo=ctx.repo_name,
            head_sha=ctx.head_sha,
            pr_number=ctx.pr_number,
        )
        if result_map["result"] == "publish_failed":
            # Pre-#550 a failed publish raised past the compliance block,
            # so the advisory never ran on this path — preserve that flow
            # (a broken Checks API makes the compliance comment-post
            # pointless anyway). The seam already logged
            # `tpm_publish_failed` and recorded the errored Activity row.
            return {"persona": "tpm", "result": "publish_failed"}
        # Ticket-compliance advisory (#529): best-effort, AFTER the DoR
        # verdict is published, in its own token+error boundary so a
        # compliance hiccup never affects the DoR result or the personas
        # dispatched after Chief. Advisory only - posts a comment, no gate.
        try:
            from github_app_auth import with_install_token_retry  # type: ignore
            from personas.tpm.ticket_compliance_run import run_ticket_compliance  # type: ignore
            result = with_install_token_retry(
                ctx.installation_id,
                lambda token: run_ticket_compliance(
                    token, owner=ctx.owner, repo=ctx.repo_name,
                    pr_number=ctx.pr_number, pr_body=ctx.pr_body,
                ),
            )
            log.info("tpm_ticket_compliance", extra={"pr_number": ctx.pr_number, "result": result})
        except Exception as e:  # noqa: BLE001 - advisory must never break the verdict
            log.warning(
                "tpm_ticket_compliance_failed",
                extra={"pr_number": ctx.pr_number, "kind": type(e).__name__},
            )
        return {
            "persona": "tpm",
            "result": result_map["result"],
        }
    except Exception as e:  # noqa: BLE001 - final guard
        # Broad catch mirrors the async Elder worker's final-guard pattern
        # (async_dispatch.run_elder_job). Without it, an unexpected
        # exception in evaluate_pull_request (or its import) would
        # propagate up and skip the personas dispatched after this one.
        # exc_info=True carries the traceback to DD/error tracking.
        log.error(
            "tpm_dispatch_unhandled",
            extra={
                "installation_id": ctx.installation_id,
                "owner": ctx.owner,
                "repo": ctx.repo_name,
                "pr_number": ctx.pr_number,
                "kind": type(e).__name__,
            },
            exc_info=True,
        )
        return {"persona": "tpm", "result": "unhandled_error"}
