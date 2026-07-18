"""Shared publish-and-record seam for persona check verdicts (#549, epic #548).

Every persona that posts a GitHub check-run ran the same construct-publish-
classify-record-return tail by hand (~30 lines, duplicated across 6 sites:
`tpm/persona.py`, `warder/dispatch.py`, `code_reviewer/dispatch.py`,
`guard/dispatch.py`, `smasher/dispatch.py`, `webhook/cave_fallback.py`).
This module owns that tail so the ADR-0003 "no lies" contract — a
degraded/failed publish still leaves an honest, recomputable Activity row,
never a silent gap — lives in ONE tested place instead of being re-derived
per persona.

Migration status per persona: see DESIGN.md "Shared publish-and-record
seam" (kept there, not here, so this docstring cannot rot per-slice).
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from activity_log import record_check_verdict
from github_app_auth import with_install_token_retry
from github_checks_client import CheckConclusion, CheckRunResult, post_check_run

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.publish_check")

# Transient-network retry budget (#697). A one-time DNS blip on the webhook
# pod (httpx ConnectError, digital-ledger#204 2026-07-18) left Chief's and
# Guard's check-runs permanently un-posted - GitHub showed the REQUIRED
# check stuck on "Expected - Waiting for status" with no self-healing,
# because this synchronous publish path had no retry (Elder alone recovered,
# via its durable queue). Budget is deliberately small: Chief publishes
# inline in the webhook handler BEFORE the HTTP 200, and GitHub's delivery
# timeout is 10s - two quick retries catch sub-second blips without risking
# the delivery ACK. httpx.TransportError covers connect/DNS/read/write/
# timeout failures but NOT HTTPStatusError - a real 4xx/5xx response from
# GitHub still fails fast (retrying those wastes the ACK window for no win).
_TRANSIENT_RETRIES = int(os.getenv("GRUG_PUBLISH_TRANSIENT_RETRIES", "2"))
_TRANSIENT_BACKOFF_BASE_S = float(os.getenv("GRUG_PUBLISH_RETRY_BASE_SECONDS", "0.5"))


def _emit_retry_exhausted_gauge(persona_key: str) -> None:
    """Best-effort DD signal that the transient-retry budget ran out (#697)
    - distinct from the check_publish_failed error log so a monitor can
    key on 'transient outage outlasted the budget' specifically."""
    try:
        from observability import emit_gauge  # type: ignore
        emit_gauge(
            "grug.check_publish.transient_retries_exhausted", 1,
            tags={"persona": persona_key},
        )
    except Exception:  # noqa: BLE001 - telemetry never breaks the publish path
        pass


def _publish_with_transient_retry(
    persona_key: str, installation_id: int, publish_fn,
) -> None:
    """Run `publish_fn` (the token-retry-wrapped check-run POST), retrying
    ONLY transport-level failures (DNS, connect, timeout) up to the small
    bounded budget above. HTTPStatusError and every non-httpx exception
    propagate immediately - the outer publish boundary owns those."""
    attempt = 0
    while True:
        try:
            publish_fn()
            return
        except httpx.TransportError as e:
            attempt += 1
            if attempt > _TRANSIENT_RETRIES:
                _emit_retry_exhausted_gauge(persona_key)
                raise
            delay = _TRANSIENT_BACKOFF_BASE_S * (2 ** (attempt - 1))
            log.warning(
                "check_publish_transient_retry",
                extra={
                    "persona": persona_key,
                    "installation_id": installation_id,
                    "attempt": attempt,
                    "max_retries": _TRANSIENT_RETRIES,
                    "delay_s": delay,
                    "kind": type(e).__name__,
                },
            )
            time.sleep(delay)

# Reserved: the seam's failure sentinel (returned + recorded when the
# check-run publish itself fails). A caller's own `success_result` must
# never collide with it, or a clean publish becomes indistinguishable
# from a real publish failure in the Activity row. PUBLIC on purpose
# (#550 stage-1 audit): every migrated persona compares the returned
# `result` against this value — importing the constant instead of
# re-typing the literal means a rename/typo cannot silently route
# publish failures onto the success path.
PUBLISH_FAILED = "publish_failed"


def publish_persona_check(
    *,
    persona_key: str,
    persona_prefix: str,
    check_name: str,
    installation_id: int,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    conclusion: CheckConclusion,
    title: str,
    summary: str,
    findings_count: int,
    blocking: bool,
    degraded_reason: str | None,
    success_result: str,
    publish_failed_log_name: str,
) -> dict[str, str]:
    """Publish one persona's check-run and record its Check verdict.

    Callers have already derived the verdict fields (`conclusion`, `title`,
    `summary`, `findings_count`, `blocking`, `degraded_reason`) — this seam
    does NOT compute them (that stays the persona's / #511's job). It owns
    only the tail: build the `CheckRunResult` + `external_id`, publish via
    the token-retry transport, classify a publish failure into ONE
    `check_publish_failed` signal (logged under `publish_failed_log_name`,
    a caller-supplied string, so DD monitors keep keying on the exact name
    each persona used before migration), perform the single honest-verdict
    merge, and call `record_check_verdict` best-effort on both the success
    and failure paths.

    Returns `{"persona": persona_key, "result": ...}` — the caller's own
    `success_result` on a clean publish, `"publish_failed"` when publishing
    failed for ANY reason (a bad HTTP response to the check-run POST, or a
    raise anywhere earlier in the token-retry/auth chain — token exchange,
    JWT signing, the SSM secret fetch, or a malformed response body).

    Raises:
        ValueError: if `success_result == "publish_failed"` — that string
            is the seam's reserved failure sentinel (exported as
            `PUBLISH_FAILED`) and cannot be reused as a caller's success
            signal. Also propagates uncaught
            from `CheckRunResult`'s own cross-field invariant (status vs.
            conclusion) — a caller-contract violation, checked before any
            network call, that should fail loud rather than be folded into
            a "publish_failed" result.
    """
    if success_result == PUBLISH_FAILED:
        raise ValueError(
            f"success_result cannot be {PUBLISH_FAILED!r} — that "
            "string is this seam's reserved failure sentinel "
            "(exported as PUBLISH_FAILED)",
        )

    full_repo = f"{owner}/{repo}"
    pr_ref = f"{full_repo}#{pr_number}"

    check_result = CheckRunResult(
        name=check_name,
        head_sha=head_sha,
        status="completed",
        conclusion=conclusion,
        title=title,
        summary=summary,
    )
    external_id = f"grug-{persona_prefix}:{full_repo}#{pr_number}:{head_sha}"

    publish_failed = False
    try:
        _publish_with_transient_retry(
            persona_key,
            installation_id,
            lambda: with_install_token_retry(
                installation_id,
                lambda token: post_check_run(
                    token, owner, repo, check_result, external_id=external_id,
                ),
            ),
        )
    except Exception as e:  # noqa: BLE001 — this IS the total publish boundary
        # every persona hand-rolled the same way (guard/warder/smasher/
        # code_reviewer all use httpx-only excepts here today); a narrower
        # catch would let a token-exchange RuntimeError, a botocore SSM
        # error, JWT signing failure, or a malformed-JSON response escape
        # uncaught and skip record_check_verdict entirely — exactly the
        # silent Activity-row gap this seam exists to close (confirmed live
        # via runtime-trace audit on PR #562: each of those failure modes
        # left `record_check_verdict` never called before this fix).
        # exc_info: pre-migration, non-httpx failures escaped to each
        # persona's final guard which logged the full traceback; the
        # seam's total boundary absorbs them first, so it must carry the
        # traceback itself or DD error tracking loses the stack frame
        # (#550 stage-2 audit). head_sha likewise: the failure line must
        # be self-sufficient even if INFO logs are sampled away.
        log.error(
            publish_failed_log_name,
            extra={
                "installation_id": installation_id,
                "pr": pr_ref,
                "head_sha": head_sha[:8],
                "kind": type(e).__name__,
                "status_code": getattr(getattr(e, "response", None), "status_code", None),
                "error": str(e)[:500],
            },
            exc_info=True,
        )
        publish_failed = True

    # Defense-in-depth: record_check_verdict is already documented as
    # never-raise (activity_log.py swallows + logs internally), but the
    # seam's own docstring promises this call can't crash the tail after
    # a successful publish — don't let that promise depend transitively on
    # a callee's internals never regressing (same discipline as
    # code_reviewer/dispatch.py's submit_evals wrap). Logged under a name
    # distinct from activity_log.py's own "check_verdict_record_failed" —
    # reusing that name would make it impossible to tell "the store write
    # itself failed" (activity_log's own, expected, WARNING-level case)
    # from "something escaped record_check_verdict's never-raise contract
    # entirely" (this one — always unexpected).
    try:
        record_check_verdict(
            install_id=installation_id,
            persona_key=persona_key,
            repo=full_repo,
            pr_number=pr_number,
            head_sha=head_sha,
            conclusion=conclusion,
            summary=title,
            findings_count=findings_count,
            blocking=blocking,
            degraded_reason=(
                degraded_reason or ("check_publish_failed" if publish_failed else None)
            ),
        )
    except Exception as e:  # noqa: BLE001 — best-effort per this module's own contract
        log.exception(
            "check_verdict_record_failed_unexpected",
            extra={
                "persona": persona_key,
                "pr": pr_ref,
                "kind": type(e).__name__,
                "error": str(e)[:500],
            },
        )

    return {
        "persona": persona_key,
        "result": PUBLISH_FAILED if publish_failed else success_result,
    }
