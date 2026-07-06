"""Shared publish-and-record seam for persona check verdicts (#549, epic #548).

Every persona that posts a GitHub check-run ran the same construct-publish-
classify-record-return tail by hand (~30 lines, duplicated across 6 sites:
`tpm/persona.py`, `warder/dispatch.py`, `code_reviewer/dispatch.py`,
`guard/dispatch.py`, `smasher/dispatch.py`, `webhook/cave_fallback.py`).
This module owns that tail so the ADR-0003 "no lies" contract — a
degraded/failed publish still leaves an honest, recomputable Activity row,
never a silent gap — lives in ONE tested place instead of being re-derived
per persona.

This slice only ADDS the seam; no persona is migrated onto it yet (#550,
#551, #552 do that). See DESIGN.md "Shared publish-and-record seam".
"""
from __future__ import annotations

import logging
import os

import httpx

from activity_log import record_check_verdict
from github_app_auth import with_install_token_retry
from github_checks_client import CheckConclusion, CheckRunResult, post_check_run

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.publish_check")


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
    `success_result` on a clean publish, `"publish_failed"` when the
    check-run POST failed.
    """
    check_result = CheckRunResult(
        name=check_name,
        head_sha=head_sha,
        status="completed",
        conclusion=conclusion,
        title=title,
        summary=summary,
    )
    external_id = f"grug-{persona_prefix}:{owner}/{repo}#{pr_number}:{head_sha}"

    publish_failed = False
    try:
        with_install_token_retry(
            installation_id,
            lambda token: post_check_run(
                token, owner, repo, check_result, external_id=external_id,
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            publish_failed_log_name,
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo}#{pr_number}",
                "kind": type(e).__name__,
            },
        )
        publish_failed = True

    record_check_verdict(
        install_id=installation_id,
        persona_key=persona_key,
        repo=f"{owner}/{repo}",
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

    return {
        "persona": persona_key,
        "result": "publish_failed" if publish_failed else success_result,
    }
