"""Scheduled reaction-poll Lambda entry point (#247b).

An EventBridge cron invokes this every ~15 min (NOT the webhook HTTP path —
there's no FastAPI/Mangum, no signature check; the trigger is IAM-gated
EventBridge). Per allowlisted install it polls 👍/👎 reactions on Grug review
comments and submits `human_verdict` DD LLM Obs evals — the human ground-truth
that calibrates the LLM judge.

Reuses the webhook container image (same `reactions` / `install_store` /
`llm_client` / `github_app_auth` code); the `scheduled_lambda` Pulumi
component (#261) points `DD_LAMBDA_HANDLER` at `poller_handler.handler` (the
image CMD stays the `datadog_lambda` wrapper, which dispatches to it).

Best-effort by construction: one install's failure (GH 5xx, token error) logs
and continues — a single bad install must never abort the whole poll cycle.
The reaction engine itself dedups via `CommentRecord.last_verdict`, so a
stale verdict isn't re-submitted every cycle.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from adapters.install_store import (  # type: ignore
    list_allowlisted_installs,
    list_comment_records,
)
from github_app_auth import with_install_token_retry
from personas.code_reviewer.reactions import poll_and_annotate

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.poller")


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    """Poll reactions for every allowlisted install. Returns a summary
    dict (installs scanned, records polled, verdicts submitted) — also
    the structured-log payload an operator/DD reads to confirm the cron
    ran end-to-end."""
    installs = list_allowlisted_installs()
    polled_records = 0
    submitted = 0
    failed_installs = 0

    for install_id in installs:
        # The ENTIRE per-install body — the CommentRecord listing AND the
        # poll — is inside this try, so a DDB listing failure for one install
        # can't abort the cron either (best-effort per install).
        # `with_install_token_retry` is used here for its token ACQUISITION;
        # its 401-refresh path is intentionally unreachable from the poller —
        # `poll_and_annotate` catches per-record GH 401s internally (best-
        # effort), so none propagates back to trigger a refresh. A revoked
        # cached token therefore self-heals on a later cron tick once the
        # token-cache TTL expires, not mid-cycle. Acceptable for best-effort
        # calibration data; surfacing first-call 401s would be #245a engine
        # surgery for marginal benefit.
        try:
            records = list_comment_records(install_id)
            if not records:
                continue
            polled_records += len(records)
            submitted += with_install_token_retry(
                install_id,
                lambda token: poll_and_annotate(
                    records,
                    install_id=install_id,
                    fetch_token=lambda: token,
                ),
            ) or 0
        except Exception as e:  # noqa: BLE001 — per-install best-effort: one
            # install's listing/GH/token failure must not abort the cron cycle.
            log.warning(
                "reaction_poll_install_failed",
                extra={"install_id": install_id, "kind": type(e).__name__},
            )
            failed_installs += 1

    result = {
        "installs": len(installs),
        "records": polled_records,
        "submitted": submitted,
        "failed_installs": failed_installs,
    }
    # Total failure (auth/config drift, GitHub down) errors EVERY install and
    # would otherwise look identical to a healthy idle cycle (submitted:0) —
    # both are `info`. Escalate the all-failed case to `error` so a
    # `status:error` monitor fires; the #261 infra slice arms the monitor.
    if installs and failed_installs == len(installs):
        log.error("reaction_poll_all_installs_failed", extra=result)
    else:
        log.info("reaction_poll_cycle_complete", extra=result)
    return result
