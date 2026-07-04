# MIRRORED — sibling at services/webhook/personas/smasher/dispatch.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Smasher persona dispatch (#469, epic #464 slice 5, ADR-0013).

The execution-class tracer: diff-scoped MUTATION TESTING. Fetch the PR diff,
extract the added Python lines, launch ONE locked-down k8s Job that mutates
those lines and runs the repo's own tests per mutant, and publish the SURVIVED
mutants (tests still pass = a coverage gap with a reproducer) as advisory
`Finding`s through the SHARED Guard/Elder publish path.

Author code NEVER runs in this pod - it runs only inside the sandbox Job
(`trial_runner`, webhook-only, lazy-imported below). Advisory-only (no blocking
flag); same never-raise degrade-to-neutral contract as Elder/Guard: any fetch /
sandbox / publish failure degrades to a neutral advisory check ("no lies",
ADR-0003).

Two-key enable (defense in depth for running untrusted code): the global master
switch `secrets_loader.get_smasher_enabled()` AND the per-repo `smasher_enabled`
toggle (checked upstream in the dispatch loop) must BOTH be on.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from activity_log import record_check_verdict
from github_app_auth import get_scoped_install_token, with_install_token_retry
from github_checks_client import CheckRunResult, post_check_run
from github_reviews_client import post_review
from personas.code_reviewer.diff_parser import DiffParseError, parse_diff
from personas.code_reviewer.dispatch import (
    ReviewMode,
    _build_review_result,
    _fetch_pr_diff,
    _prior_finding_keys,
    _publish_shape,
    _resolve_result,
)
from personas.code_reviewer.persona import (
    CodeReviewEvaluation,
    Finding,
    with_findings,
)
from personas.smasher.sandbox import SurvivedMutant, TrialResult, extract_target_lines

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.smasher")

_CHECK_NAME = "Grug — Smasher"

# Trial budgets (ADR-0013 kill switches). The total budget is ALSO enforced by
# the Job's activeDeadlineSeconds (the kubelet-level hard stop); these bound the
# work the mutation worker attempts.
_MUTANT_CAP = 10
_PER_MUTANT_TIMEOUT_SECONDS = 30
_TOTAL_BUDGET_SECONDS = 600


def _summary_markdown(evaluation: CodeReviewEvaluation) -> tuple[str, str]:
    """Smasher-voice (title, summary) for the check-run output."""
    if evaluation.degraded_reason:
        return (
            f"⚠️ Smasher club not swing ({evaluation.degraded_reason})",
            "Grug Smasher could not run the trial this time. The trouble: "
            f"`{evaluation.degraded_reason}`. This only counsel - merge not blocked.",
        )
    if not evaluation.findings:
        return (
            "✅ Smasher find no weak test",
            "Grug Smasher change the new code many ways and the tribe's tests "
            "still catch every change. Tests strong. Smasher nod.",
        )
    rows = ["| File | Line | Mutation | Grug say |", "|---|---|---|---|"]
    for f in evaluation.findings:
        rows.append(f"| `{f.file}` | {f.line} | {f.rule_name} | {f.message} |")
    return (
        f"❌ Smasher slip {len(evaluation.findings)} change past the tests",
        "Grug Smasher change the new code and the tests still pass - a test "
        "is missing. Each row is a mutation no test caught:\n\n" + "\n".join(rows),
    )


def _finding_for(mutant: SurvivedMutant) -> Finding:
    """One survived mutant -> an advisory `Finding`. `low` severity keeps it
    advisory (Smasher never blocks); the message is a concrete reproducer."""
    return Finding(
        file=mutant.file,
        line=mutant.line,
        severity="low",
        rule_name="surviving-mutant",
        message=(
            f"Mutation `{mutant.original}` -> `{mutant.mutated}` "
            f"({mutant.operator}) on this line did not fail any test - "
            "the new code is under-tested here. Add a test that this change breaks."
        ),
        suggestion=None,
    )


def _publish_degraded(
    installation_id: int, owner: str, repo: str, pull_number: int,
    head_sha: str, *, reason: str,
) -> dict[str, str]:
    """Neutral Smasher check-run for a run that could not evaluate. Best-effort."""
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
                external_id=f"grug-smasher:{owner}/{repo}#{pull_number}:{head_sha}",
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            "smasher_degraded_publish_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
    record_check_verdict(
        install_id=installation_id, persona_key="smasher",
        repo=f"{owner}/{repo}", pr_number=pull_number, head_sha=head_sha,
        conclusion="neutral", summary=title, findings_count=0,
        blocking=False, degraded_reason=reason,
    )
    return {"persona": "smasher", "result": "degraded"}


def dispatch_smasher_review(
    payload: dict[str, Any], *, blocking: bool,
) -> dict[str, str]:
    """Entry point - one Smasher Trial pass. `blocking` is always False
    (Smasher has no blocking mode). Never raises; every failure degrades to an
    advisory neutral check."""
    mode: ReviewMode = "advisory"  # Smasher is advisory-only
    action = payload.get("action", "")
    pr = payload["pull_request"]
    repo = payload["repository"]
    installation = payload["installation"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    pull_number = int(pr["number"])
    head_sha = pr["head"]["sha"]
    installation_id = int(installation["id"])

    # Global master kill switch (ADR-0013): OFF unless the operator explicitly
    # enabled Smasher account-wide. Per-repo opt-in was already checked upstream.
    from secrets_loader import get_smasher_enabled  # lazy: rare path, uncached

    if not get_smasher_enabled():
        log.info(
            "smasher_globally_disabled",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}"},
        )
        return {"persona": "smasher", "result": "disabled_global"}

    # Fetch + parse the diff.
    try:
        diff_text = with_install_token_retry(
            installation_id,
            lambda token: _fetch_pr_diff(token, owner, repo_name, pull_number),
        )
        hunks = parse_diff(diff_text)
    except (httpx.HTTPStatusError, httpx.RequestError, DiffParseError) as e:
        log.warning(
            "smasher_fetch_or_parse_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        return _publish_degraded(
            installation_id, owner, repo_name, pull_number, head_sha,
            reason="fetch_or_parse_failed",
        )

    targets = extract_target_lines(hunks)
    if not targets:
        # No changed Python lines to mutate -> a clean advisory pass.
        return _publish_clean(installation_id, owner, repo_name, pull_number, head_sha)

    # Mint a DOWN-SCOPED, single-repo, contents:read token for the sandbox
    # clone (ADR-0013). Never the full-scope cached token.
    try:
        scoped_token = get_scoped_install_token(
            installation_id,
            repositories=[repo_name],
            permissions={"contents": "read"},
        )
    except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
        log.warning(
            "smasher_scoped_token_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        return _publish_degraded(
            installation_id, owner, repo_name, pull_number, head_sha,
            reason="scoped_token_failed",
        )

    # Launch the locked-down Trial Job (webhook-only; lazy import keeps this
    # mirrored module importable in the api service where it never runs).
    try:
        from personas.smasher.trial_runner import launch_trial

        result: TrialResult = launch_trial(
            owner=owner,
            repo=repo_name,
            head_sha=head_sha,
            token=scoped_token,
            targets=targets,
            mutant_cap=_MUTANT_CAP,
            per_mutant_timeout_seconds=_PER_MUTANT_TIMEOUT_SECONDS,
            total_budget_seconds=_TOTAL_BUDGET_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 — a sandbox failure degrades, never raises
        log.warning(
            "smasher_trial_launch_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        return _publish_degraded(
            installation_id, owner, repo_name, pull_number, head_sha,
            reason="trial_launch_failed",
        )

    if result.status != "completed":
        return _publish_degraded(
            installation_id, owner, repo_name, pull_number, head_sha,
            reason="trial_degraded",
        )

    findings = tuple(_finding_for(m) for m in result.survived)
    evaluation = with_findings(
        CodeReviewEvaluation(findings=(), conclusion="success"), findings
    )
    log.info(
        "smasher_trial_done",
        extra={
            "installation_id": installation_id,
            "pr": f"{owner}/{repo_name}#{pull_number}",
            "total": result.total, "killed": result.killed,
            "survived": len(result.survived), "timed_out": result.timed_out,
        },
    )
    return _publish(
        installation_id, owner, repo_name, pull_number, head_sha,
        evaluation, mode=mode, action=action,
    )


def _publish_clean(
    installation_id: int, owner: str, repo: str, pull_number: int, head_sha: str,
) -> dict[str, str]:
    """Advisory pass check when the PR changed no Python lines to mutate."""
    evaluation = CodeReviewEvaluation(findings=(), conclusion="success")
    return _publish(
        installation_id, owner, repo, pull_number, head_sha,
        evaluation, mode="advisory", action="",
    )


def _publish(
    installation_id: int, owner: str, repo: str, pull_number: int, head_sha: str,
    evaluation: CodeReviewEvaluation, *, mode: ReviewMode, action: str,
) -> dict[str, str]:
    """Publish the Smasher check-run + inline review on independent surfaces
    (same dual-publish discipline as Guard/Elder) and record the verdict."""
    conclusion, event = _publish_shape(evaluation, mode=mode)
    title, summary = _summary_markdown(evaluation)
    check_publish_failed = False
    try:
        with_install_token_retry(
            installation_id,
            lambda token: post_check_run(
                token, owner, repo,
                CheckRunResult(
                    name=_CHECK_NAME, head_sha=head_sha, status="completed",
                    conclusion=conclusion, title=title, summary=summary,
                ),
                external_id=f"grug-smasher:{owner}/{repo}#{pull_number}:{head_sha}",
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            "smasher_check_run_publish_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        check_publish_failed = True

    review_publish_failed = False
    prior_keys: frozenset[str] = frozenset()
    if action in {"synchronize", "reopened"}:
        prior_keys, _dedup_degraded = _prior_finding_keys(
            installation_id, owner, repo, pull_number,
        )
    review_result = _build_review_result(
        evaluation, head_sha=head_sha, event=event, prior_keys=prior_keys,
    )
    if review_result is not None:
        try:
            with_install_token_retry(
                installation_id,
                lambda token: post_review(
                    token, owner, repo,
                    pull_number=pull_number, result=review_result,
                ),
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.error(
                "smasher_review_publish_failed",
                extra={
                    "installation_id": installation_id,
                    "pr": f"{owner}/{repo}#{pull_number}",
                    "kind": type(e).__name__,
                },
            )
            review_publish_failed = True

    record_check_verdict(
        install_id=installation_id, persona_key="smasher",
        repo=f"{owner}/{repo}", pr_number=pull_number, head_sha=head_sha,
        conclusion=conclusion, summary=title,
        findings_count=len(evaluation.findings), blocking=False,
        degraded_reason=(
            evaluation.degraded_reason
            or ("check_publish_failed" if check_publish_failed else None)
        ),
    )
    return {
        "persona": "smasher",
        "result": _resolve_result(
            evaluation,
            check_publish_failed=check_publish_failed,
            review_publish_failed=review_publish_failed,
        ),
    }
