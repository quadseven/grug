"""TPM persona — pure DoR rollup + GitHub Checks publisher.

Per spec 0002 (`evaluate_pull_request_is_pure_function_per_process_gate_concepts`):
`evaluate_pull_request(pr_body)` is pure (no IO). The GitHub POST lives
in `publish_tpm_evaluation(evaluation, *, ...)`, which is the only
impure surface. Split lets us replay/fuzz/test the rollup without
GitHub or AWS round-trips, and lets the spec's purity attestation
actually be true.

Publishing goes through the shared `publish_persona_check` seam (#549/#550),
which owns the token-retry transport, the publish-failure classification,
and the honest `record_check_verdict` call on BOTH paths — so a failed
check-run POST still leaves an errored Activity row (ADR-0003 "no lies").
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from github_checks_client import CheckConclusion
from personas.publish_check import publish_persona_check
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

# DD_SERVICE-derived namespace (grug-api / grug-webhook) — the same
# convention every other shared module uses. Pre-extraction this was the
# one hardcoded per-service divergence in the mirror set (ADR-0014).
log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.tpm")

_CHECK_NAME = "Grug — Definition of Ready"
_ADVISORY_CHECKS: frozenset[str] = frozenset({"issue-link"})


def _summary(results: list[CheckResult]) -> tuple[str, str]:
    """Build (title, summary) markdown for the check-run output."""
    blocking = [r for r in results if not r.passed and r.name not in _ADVISORY_CHECKS]
    title = (
        f"✅ DoR pass — all {len(results)} checks"
        if not blocking
        else f"❌ DoR fail — {len(blocking)}/{len(results)} blocking"
    )
    lines = ["| Check | Status | Detail |", "|---|---|---|"]
    for r in results:
        if r.passed:
            icon = "✅"
        elif r.name in _ADVISORY_CHECKS:
            icon = "⚠️"
        else:
            icon = "❌"
        lines.append(f"| {r.name} | {icon} | {r.detail} |")
    return title, "\n".join(lines)


def evaluate_pull_request(pr_body: str) -> TpmEvaluation:
    """Pure: run all 5 DoR rules over pr_body and return the rollup.

    No network IO, no AWS calls, no logging side-effects. Callers wrap
    the result in `publish_tpm_evaluation(...)` to POST the check-run.
    """
    results = run_all(pr_body)
    blocking = [r for r in results if not r.passed and r.name not in _ADVISORY_CHECKS]
    conclusion: CheckConclusion = "success" if not blocking else "failure"
    return TpmEvaluation(
        passed=not blocking,
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
) -> dict[str, str]:
    """Impure: POST `evaluation` to GitHub's Checks API via the shared seam.

    The seam (`publish_persona_check`, #549/#550) owns the token-retry
    transport (incl. the 401-revoked-token retry that used to live here,
    Codex post-review #50), classifies ANY publish failure into one
    `tpm_publish_failed` signal, and records the Check verdict on both
    paths — a publish failure now leaves an honest errored Activity row
    with `degraded_reason="check_publish_failed"` instead of no row at
    all (the pre-#550 gap). Returns `{"persona": "tpm", "result": ...}`
    where result is "pass"/"fail" on a clean publish, "publish_failed"
    otherwise.
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
    # Chief's `findings_count` is the number of failed BLOCKING DoR checks
    # (0 on pass) — advisory checks (issue-link) don't gate, so they don't
    # count toward the block/pass verdict. TPM never degrades at the eval
    # layer (conclusion is success|failure), so `degraded_reason` stays
    # None; only the seam's publish-failure classification can set one.
    result_map = publish_persona_check(
        persona_key="tpm",
        persona_prefix="tpm",
        check_name=_CHECK_NAME,
        installation_id=installation_id,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        conclusion=evaluation.conclusion,
        title=title,
        summary=summary,
        findings_count=sum(
            1 for r in evaluation.results
            if not r.passed and r.name not in _ADVISORY_CHECKS
        ),
        blocking=True,
        degraded_reason=None,
        success_result="pass" if evaluation.passed else "fail",
        publish_failed_log_name="tpm_publish_failed",
    )
    if result_map["result"] != "publish_failed":
        # Static event name — DD monitors key on it. The failure-path
        # outcome log is the seam's `tpm_publish_failed` (same event name
        # the dispatcher emitted pre-migration), so `tpm_published` fires
        # ONLY on a real publish.
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
    return result_map
