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
from collections.abc import Sequence
from dataclasses import dataclass

from github_checks_client import CheckConclusion
from personas.publish_check import PUBLISH_FAILED, publish_persona_check
from personas.tpm.dor_checks import CheckResult, IssueFactsFetcher, run_all
from personas.tpm.dor_checks import IssueFetcher
from personas.tribe import CHECK_CHIEF


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

    def __post_init__(self) -> None:
        # passed and conclusion are two encodings of the same rollup —
        # since #550 they feed the publish seam as INDEPENDENT params
        # (`conclusion` -> the check-run + Activity row, `passed` ->
        # `success_result`), so an incoherent hand-built instance would
        # publish a red check while returning "pass" to the dispatcher.
        # Same construction-boundary discipline as CheckRunResult's
        # status/conclusion invariant.
        if self.passed != (self.conclusion == "success"):
            raise ValueError(
                f"TpmEvaluation incoherent: passed={self.passed} but "
                f"conclusion={self.conclusion!r}",
            )

# DD_SERVICE-derived namespace (grug-api / grug-webhook) — the same
# convention every other shared module uses. Pre-extraction this was the
# one hardcoded per-service divergence in the mirror set (ADR-0014).
log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.tpm")

_CHECK_NAME = CHECK_CHIEF
_ADVISORY_CHECKS: frozenset[str] = frozenset({"issue-link"})


def _blocking_failures(results: Sequence[CheckResult]) -> list[CheckResult]:
    """Failed BLOCKING checks — the one predicate behind title, rollup,
    and `findings_count`. Advisory checks (issue-link) don't gate, so
    they never count; keeping the predicate in one place stops the
    three call sites from drifting (#550 stage-1 audit)."""
    return [r for r in results if not r.passed and r.name not in _ADVISORY_CHECKS]


# DISPLAY ONLY - the key stays. `CheckResult.name` is load-bearing:
# `_ADVISORY_CHECKS` keys off it to decide blocking-vs-warn, and Chief's
# issue-time remedy text keys off it too. Renaming the values would silently
# make `issue-link` blocking. Same split ADR-0002 draws for personas: the
# caveman name is what a human reads, the key is what code matches on.
_CHECK_DISPLAY: dict[str, str] = {
    "why": "why hunt",
    "acceptance": "what good look like",
    "estimate": "how big",
    "scope-fence": "where stop",
    "issue-link": "which ticket",
    "linked-issue-completeness": "ticket done",
    "linked-issue-epic": "which epic",
}


def check_display_name(key: str) -> str:
    """Caveman label for a check, falling back to the key.

    Falls back rather than raising: a check added without a label should show
    up looking plain, not take the whole Hunt Plan summary down with it.
    """
    return _CHECK_DISPLAY.get(key, key)


def _skipped(results: Sequence[CheckResult]) -> list[CheckResult]:
    """Checks that passed by fail-open without being evaluated (#782)."""
    return [r for r in results if r.skipped]


def _summary(results: list[CheckResult]) -> tuple[str, str]:
    """Build (title, summary) markdown for the check-run output.

    A skipped check (fail-open, #782) never blocks, but it is named in the
    title and dropped from the "all N" claim: "all 6 checks" over a check
    that never ran is exactly the silent pass the fail-open branch was
    never meant to be. `Hunt Plan ready - 5/6 checks (ticket done skipped)`
    tells the reader which row to distrust.
    """
    blocking = _blocking_failures(results)
    skipped = _skipped(results)
    total = len(results)
    if not blocking:
        title = f"Hunt Plan ready - all {total} checks"
        if skipped:
            title = f"Hunt Plan ready - {total - len(skipped)}/{total} checks"
    else:
        # The per-check breakdown lives only in this check-run's own
        # `output.summary` (the table built below) - GitHub's PR checks
        # list shows just this title inline, so a reader who doesn't
        # click into "Details" sees a fail with no idea why. Relayed
        # live: three agents mistook a run of these for an outage before
        # finding the summary was the only place the reason lived.
        title = (
            f"Hunt Plan hold - {len(blocking)}/{total} plan checks fail "
            "- see Details for which"
        )
    if skipped:
        names = ", ".join(check_display_name(r.name) for r in skipped)
        title += f" ({names} skipped)"
    lines = ["| Check | Status | Detail |", "|---|---|---|"]
    for r in results:
        if r.skipped:
            icon = "skipped"
        elif r.passed:
            icon = "pass"
        elif r.name in _ADVISORY_CHECKS:
            icon = "warn"
        else:
            icon = "fail"
        lines.append(f"| {check_display_name(r.name)} | {icon} | {r.detail} |")
    return title, "\n".join(lines)


def evaluate_pull_request(
    pr_body: str,
    *,
    fetch_issue: IssueFetcher | None = None,
    fetch_issue_facts: IssueFactsFetcher | None = None,
) -> TpmEvaluation:
    """Pure: run all 7 DoR rules over pr_body and return the rollup.

    No network IO, no AWS calls, no logging side-effects. Callers wrap
    the result in `publish_tpm_evaluation(...)` to POST the check-run.

    `fetch_issue` is passed through to check_linked_issue_completeness.
    When None, that check fails open (pass, marked `skipped` so the
    rendered title does not count it - #782). Real callers build one via
    `personas.tpm.issue_fetcher.build_issue_fetcher`. The fetcher is a
    parameter, not a call target inside this function -- the purity
    attestation (attest_persona_purity.py) sees only the allowlisted
    `run_all` call.
    """
    results = run_all(
        pr_body, fetch_issue=fetch_issue, fetch_issue_facts=fetch_issue_facts,
    )
    blocking = _blocking_failures(results)
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
        findings_count=len(_blocking_failures(evaluation.results)),
        blocking=True,
        degraded_reason=None,
        success_result="pass" if evaluation.passed else "fail",
        publish_failed_log_name="tpm_publish_failed",
    )
    if result_map["result"] != PUBLISH_FAILED:
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
                # Fail-open checks that never ran (#782): a pass with a
                # non-empty list here is a pass that was not fully earned.
                "skipped_checks": [r.name for r in _skipped(evaluation.results)],
            },
        )
    return result_map
