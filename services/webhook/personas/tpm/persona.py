"""TPM persona evaluator — runs static DoR + posts check-run."""

from __future__ import annotations

import logging
from typing import Any

from github_app_auth import get_install_token
from github_checks_client import CheckRunResult, post_check_run
from personas.tpm.dor_checks import CheckResult, run_all

log = logging.getLogger("grug.api.persona.tpm")

_CHECK_NAME = "Grug — Definition of Ready"


def _summary(results: list[CheckResult]) -> tuple[str, str]:
    """Build (title, summary) markdown for the check-run output."""
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    title = (
        f"✅ DoR pass — all {len(results)} checks"
        if not failed
        else f"❌ DoR fail — {len(failed)}/{len(results)} blocking"
    )
    lines = ["| Check | Status | Detail |", "|---|---|---|"]
    for r in results:
        icon = "✅" if r.passed else "❌"
        lines.append(f"| {r.name} | {icon} | {r.detail} |")
    return title, "\n".join(lines)


def evaluate_pull_request(
    *,
    installation_id: int,
    owner: str,
    repo: str,
    head_sha: str,
    pr_body: str,
    pr_number: int,
) -> CheckResult:
    """Run TPM checks on the PR body + post check-run. Returns overall result."""
    results = run_all(pr_body)
    failed = [r for r in results if not r.passed]
    overall = CheckResult(
        name="overall",
        passed=not failed,
        detail=("all pass" if not failed else f"{len(failed)} blocking"),
    )
    title, summary = _summary(results)
    conclusion = "success" if not failed else "failure"

    install_token = get_install_token(installation_id)
    post_check_run(
        install_token=install_token,
        owner=owner, repo=repo,
        result=CheckRunResult(
            name=_CHECK_NAME,
            head_sha=head_sha,
            status="completed",
            conclusion=conclusion,
            title=title,
            summary=summary,
        ),
        external_id=f"grug-tpm:{owner}/{repo}#{pr_number}:{head_sha}",
    )
    log.info(
        "tpm_evaluated",
        extra={
            "installation_id": installation_id,
            "repo": f"{owner}/{repo}",
            "pr_number": pr_number,
            "head_sha": head_sha[:8],
            "passed": overall.passed,
            "failed_checks": [r.name for r in failed],
        },
    )
    return overall
