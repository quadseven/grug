"""Guard persona dispatch (#466, epic #464 slice 2, ADR-0012).

The security suite that shipped INSIDE Elder - four deterministic
candidate sources (SAST `sast.py`, dependency-CVE `sca.py`, committed
secrets `secret_scan.py`, IaC misconfig `iac_scan.py`) feeding the ONE
exploitability judge (`judge_candidates`) - now posts its OWN check-run
("Grug — Guard") with its own advisory/blocking flag. Elder keeps the
LLM diff review; users can see, toggle, and (eventually) block on
security findings separately.

OWNERSHIP BY IMPORT, not file move (ADR-0012): the detector modules stay
under `personas/code_reviewer/` so the SAST benchmark harness, the
drift-lint pair list, and every historical import keep working; Guard is
their dispatch owner. The shared fetch/publish helpers are imported from
Elder's dispatch module for the same reason - extracting a shared
package is #77's job, re-triggered by this third persona.

Advisory-first contract mirrors Elder's: `blocking=False` (default per
RepoConfig.guard_blocking) forces neutral/COMMENT; degraded runs are
ALWAYS neutral ("no lies", ADR-0003) and recorded as errored Activity
rows.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from activity_log import record_check_verdict
from github_app_auth import with_install_token_retry
from github_checks_client import CheckRunResult, post_check_run
from github_reviews_client import post_review
from llm_client import _JUDGE_MAX_FINDINGS
from personas.code_reviewer.diff_parser import DiffParseError, parse_diff
from personas.code_reviewer.dispatch import (
    ReviewMode,
    _build_review_result,
    _fetch_file_contents,
    _fetch_pr_diff,
    _prior_finding_keys,
    _publish_shape,
    _resolve_result,
)
from personas.code_reviewer.iac_scan import scan_iac
from personas.code_reviewer.persona import CodeReviewEvaluation, with_extra_findings
from personas.code_reviewer.sast import judge_candidates, scan_candidates
from personas.code_reviewer.sca import scan_dependencies
from personas.code_reviewer.secret_scan import scan_secrets

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.guard")

_CHECK_NAME = "Grug — Guard"


def _summary_markdown(evaluation: CodeReviewEvaluation) -> tuple[str, str]:
    """Guard-voice (title, summary) for the check-run output."""
    if evaluation.degraded_reason:
        title = f"⚠️ Guard eyes clouded ({evaluation.degraded_reason})"
        return title, (
            "Grug Guard could not watch the pass this time. The mist: "
            f"`{evaluation.degraded_reason}`. This only counsel — merge "
            "not blocked."
        )
    if not evaluation.findings:
        title = "✅ Guard find no evil"
        return title, (
            "Grug Guard watch the diff for leaked secrets, weak code, sick "
            "dependencies, and open doors. Nothing evil pass. Guard nod."
        )
    severity_icon = {
        "critical": "🛑", "high": "❌", "medium": "⚠️", "low": "ℹ️",
    }
    blocking = sum(
        1 for f in evaluation.findings if f.severity in ("high", "critical")
    )
    title = (
        f"❌ Guard see evil — {blocking} blocking · "
        f"{len(evaluation.findings)} finding(s) in all"
    )
    rows = ["| Severity | File | Line | Rule | Message |", "|---|---|---|---|---|"]
    for f in evaluation.findings:
        icon = severity_icon.get(f.severity, "•")
        rows.append(
            f"| {icon} {f.severity} | `{f.file}` | {f.line} | "
            f"`{f.rule_name}` | {f.message} |"
        )
    return title, "\n".join(rows)


def _publish_degraded(
    installation_id: int, owner: str, repo: str, pull_number: int,
    head_sha: str, *, reason: str,
) -> dict[str, str]:
    """Neutral Guard check-run for a run that could not evaluate ("no
    lies": degraded is never a pass). Best-effort publish."""
    evaluation = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason=reason,
    )
    title, summary = _summary_markdown(evaluation)
    try:
        with_install_token_retry(
            installation_id,
            lambda token: post_check_run(
                token, owner, repo,
                CheckRunResult(
                    name=_CHECK_NAME, head_sha=head_sha, status="completed",
                    conclusion="neutral", title=title, summary=summary,
                ),
                external_id=f"grug-guard:{owner}/{repo}#{pull_number}:{head_sha}",
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            "guard_degraded_publish_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
    return {"persona": "guard", "result": "degraded"}


def dispatch_guard_review(
    payload: dict[str, Any], *, blocking: bool,
) -> dict[str, str]:
    """Entry point - one Guard security pass. Same never-raises degrade
    contract as Elder's `dispatch_code_review`: fetch/parse failures,
    scan failures, and publish 5xx all degrade to advisory neutral."""
    mode: ReviewMode = "blocking" if blocking else "advisory"
    action = payload.get("action", "")
    pr = payload["pull_request"]
    repo = payload["repository"]
    installation = payload["installation"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    pull_number = int(pr["number"])
    head_sha = pr["head"]["sha"]
    installation_id = int(installation["id"])

    try:
        diff_text = with_install_token_retry(
            installation_id,
            lambda token: _fetch_pr_diff(token, owner, repo_name, pull_number),
        )
        hunks = parse_diff(diff_text)
    except (httpx.HTTPStatusError, httpx.RequestError, DiffParseError) as e:
        log.warning(
            "guard_fetch_or_parse_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        degraded = _publish_degraded(
            installation_id, owner, repo_name, pull_number, head_sha,
            reason="fetch_or_parse_failed",
        )
        record_check_verdict(
            install_id=installation_id,
            persona_key="guard",
            repo=f"{owner}/{repo_name}",
            pr_number=pull_number,
            head_sha=head_sha,
            conclusion="neutral",
            summary="Guard could not look — diff fetch/parse failed",
            findings_count=0,
            blocking=blocking,
            degraded_reason="fetch_or_parse_failed",
        )
        return degraded

    # Full-file context (#336) so the judge sees mitigations outside the
    # hunks - best-effort, diff-only on failure.
    changed_paths = tuple(dict.fromkeys(h.file_path for h in hunks))
    try:
        file_contents = with_install_token_retry(
            installation_id,
            lambda token: _fetch_file_contents(
                token, owner, repo_name, changed_paths, head_sha
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.info(
            "guard_file_contents_unavailable",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "error": str(e)},
        )
        file_contents = {}

    # The security pipeline, moved VERBATIM from Elder's dispatch (#466):
    # four candidate sources -> ONE exploitability-judge pass (recall from
    # the engines, precision from the judge). Secrets FIRST so a committed
    # live credential survives the judge-budget truncation on a noisy PR.
    evaluation = CodeReviewEvaluation(findings=(), conclusion="success")
    try:
        candidates = (
            scan_secrets(hunks)
            + scan_candidates(hunks, file_contents=file_contents)
            + scan_dependencies(hunks)
            + scan_iac(hunks)
        )
        if len(candidates) > _JUDGE_MAX_FINDINGS:
            log.info(
                "security_candidates_truncated_to_judge_budget",
                extra={
                    "installation_id": installation_id,
                    "total": len(candidates),
                    "max": _JUDGE_MAX_FINDINGS,
                },
            )
            candidates = candidates[:_JUDGE_MAX_FINDINGS]
        security_findings = judge_candidates(
            candidates,
            hunks,
            installation_id,
            pr_context={
                "installation_id": installation_id,
                "repo": f"{owner}/{repo_name}",
                "pr_number": pull_number,
                "head_sha": head_sha,
            },
            file_contents=file_contents,
        )
        if security_findings:
            evaluation = with_extra_findings(evaluation, security_findings)
            log.info(
                "security_findings_merged",
                extra={
                    "installation_id": installation_id,
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "count": len(security_findings),
                },
            )
    except Exception as e:  # noqa: BLE001 — the scan must not kill the check-run publish
        log.warning(
            "security_detection_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )

    # Publish: check-run + inline review on independent surfaces, same
    # dual-publish discipline as Elder. The dedup path is SHARED (Elder's
    # helpers + the grug-rule comment markers), not forked.
    conclusion, event = _publish_shape(evaluation, mode=mode)
    title, summary = _summary_markdown(evaluation)
    check_publish_failed = False
    try:
        with_install_token_retry(
            installation_id,
            lambda token: post_check_run(
                token, owner, repo_name,
                CheckRunResult(
                    name=_CHECK_NAME, head_sha=head_sha, status="completed",
                    conclusion=conclusion, title=title, summary=summary,
                ),
                external_id=f"grug-guard:{owner}/{repo_name}#{pull_number}:{head_sha}",
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            "guard_check_run_publish_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        check_publish_failed = True

    review_publish_failed = False
    prior_keys: frozenset[str] = frozenset()
    if action in {"synchronize", "reopened"}:
        prior_keys, _dedup_degraded = _prior_finding_keys(
            installation_id, owner, repo_name, pull_number,
        )
    review_result = _build_review_result(
        evaluation, head_sha=head_sha, event=event, prior_keys=prior_keys,
    )
    if review_result is not None:
        try:
            with_install_token_retry(
                installation_id,
                lambda token: post_review(
                    token, owner, repo_name,
                    pull_number=pull_number, result=review_result,
                ),
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.error(
                "guard_review_publish_failed",
                extra={
                    "installation_id": installation_id,
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "kind": type(e).__name__,
                },
            )
            review_publish_failed = True

    record_check_verdict(
        install_id=installation_id,
        persona_key="guard",
        repo=f"{owner}/{repo_name}",
        pr_number=pull_number,
        head_sha=head_sha,
        conclusion=conclusion,
        summary=title,
        findings_count=len(evaluation.findings),
        blocking=blocking,
        degraded_reason=(
            evaluation.degraded_reason
            or ("check_publish_failed" if check_publish_failed else None)
        ),
    )
    return {
        "persona": "guard",
        "result": _resolve_result(
            evaluation,
            check_publish_failed=check_publish_failed,
            review_publish_failed=review_publish_failed,
        ),
    }
