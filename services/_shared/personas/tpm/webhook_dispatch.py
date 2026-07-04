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
    import httpx  # type: ignore
    try:
        from personas.tpm.persona import (  # type: ignore
            evaluate_pull_request, publish_tpm_evaluation,
        )
        evaluation = evaluate_pull_request(ctx.pr_body)
        publish_tpm_evaluation(
            evaluation,
            installation_id=ctx.installation_id,
            owner=ctx.owner,
            repo=ctx.repo_name,
            head_sha=ctx.head_sha,
            pr_number=ctx.pr_number,
        )
        return {
            "persona": "tpm",
            "result": "pass" if evaluation.passed else "fail",
        }
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            "tpm_publish_failed",
            extra={
                "installation_id": ctx.installation_id,
                "owner": ctx.owner,
                "repo": ctx.repo_name,
                "pr_number": ctx.pr_number,
                "head_sha": ctx.head_sha[:8],
                "kind": type(e).__name__,
                "status": getattr(getattr(e, "response", None), "status_code", None),
            },
        )
        return {"persona": "tpm", "result": "publish_failed"}
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
