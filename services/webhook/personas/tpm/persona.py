"""TPM persona — pure DoR rollup + GitHub Checks publisher.

Per spec 0002 (`evaluate_pull_request_is_pure_function_per_process_gate_concepts`):
`evaluate_pull_request(pr_body)` is pure (no IO). The GitHub POST lives
in `publish_tpm_evaluation(evaluation, *, ...)`, which is the only
impure surface. Split lets us replay/fuzz/test the rollup without
GitHub or AWS round-trips, and lets the spec's purity attestation
actually be true.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from github_app_auth import with_install_token_retry
from github_checks_client import CheckConclusion, CheckRunResult, post_check_run
from personas.tpm.dor_checks import CheckResult, run_all


@dataclass(frozen=True)
class TpmEvaluation:
    """Persona-level rollup of dor_checks results.

    Distinct from CheckResult (per-check name + passed + detail) so
    callers don't have to know the magic name='overall' string. The
    `conclusion` field aligns with github_checks_client's
    CheckConclusion vocabulary so the GH POST shape stays explicit.
    Closes #104.
    """
    passed: bool
    results: tuple[CheckResult, ...]
    conclusion: CheckConclusion

# Logger uses the `grug.webhook.*` namespace; api-side copy uses
# `grug.api.*`. The single-line divergence is intentional + the
# only reason this file isn't in MIRRORED_FILES (drift-lint).
log = logging.getLogger("grug.webhook.persona.tpm")

_CHECK_NAME = "Grug — Definition of Ready"


def _summary(results: list[CheckResult]) -> tuple[str, str]:
    """Build (title, summary) markdown for the check-run output."""
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


def evaluate_pull_request(pr_body: str) -> TpmEvaluation:
    """Pure: run all 5 DoR rules over pr_body and return the rollup.

    No network IO, no AWS calls, no logging side-effects. Callers wrap
    the result in `publish_tpm_evaluation(...)` to POST the check-run.
    """
    results = run_all(pr_body)
    failed = [r for r in results if not r.passed]
    conclusion: CheckConclusion = "success" if not failed else "failure"
    return TpmEvaluation(
        passed=not failed,
        results=tuple(results),
        conclusion=conclusion,
    )


def publish_tpm_evaluation(
    evaluation: TpmEvaluation,
    *,
    installation_id: int,
    owner: str,
    repo: str,
    head_sha: str,
    pr_number: int,
) -> None:
    """Impure: POST `evaluation` to GitHub's Checks API.

    Retry once on 401 — handles tokens revoked out-of-band (App
    reinstall, perm change, secret rotation). Codex post-review #50.
    """
    title, summary = _summary(list(evaluation.results))
    log.info(
        "tpm_publishing",
        extra={
            "installation_id": installation_id,
            "repo": f"{owner}/{repo}",
            "pr_number": pr_number,
            "head_sha": head_sha[:8],
            "passed": evaluation.passed,
        },
    )
    with_install_token_retry(
        installation_id,
        lambda token: post_check_run(
            install_token=token,
            owner=owner, repo=repo,
            result=CheckRunResult(
                name=_CHECK_NAME,
                head_sha=head_sha,
                status="completed",
                conclusion=evaluation.conclusion,
                title=title,
                summary=summary,
            ),
            external_id=f"grug-tpm:{owner}/{repo}#{pr_number}:{head_sha}",
        ),
    )
    log.info(
        "tpm_published",
        extra={
            "installation_id": installation_id,
            "repo": f"{owner}/{repo}",
            "pr_number": pr_number,
            "head_sha": head_sha[:8],
            "passed": evaluation.passed,
            "failed_checks": [r.name for r in evaluation.results if not r.passed],
        },
    )
