"""The one finding funnel every Elder pass publishes through (#1586).

Tier-1, the deep reasoner, the quarantined security pass, the second
lineage and the eval replay all hand their parsed evaluation here. The
deterministic filters run first, in a fixed order, so the judge never
spends a call on a finding they drop:

1. correctness-only: a docs- or tests-only review keeps correctness rules;
2. unreachable-defensive: a low or medium defensive finding that names no
   reachable caller or input is dropped;
3. checked-shell: a shell command whose exit status is already checked
   (#771) is not "unchecked";
4. intent-mismatch: the evidence filter (#1170) and box scope (#1218).

Then the injected gates run: the plausibility judge, the repo-grounded
verifier, and last the falsification verifier (#1567) or the batched refute
gate it replaces. The gates are callables so each caller keeps its own
telemetry and backends while the order stays single-sourced here.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from llm_client import FindingJudgement

from personas.code_reviewer.intent_filter import (
    filter_intent_mismatch_findings,
    scope_intent_mismatch_to_claimed_boxes,
)
from personas.code_reviewer.judge import partition_findings
from personas.code_reviewer.linked_issue import rendered_criteria_with_state
from personas.code_reviewer.persona import (
    CodeReviewEvaluation,
    Finding,
    with_findings,
)
from personas.code_reviewer.shell_flow_filter import filter_checked_shell_findings
from personas.tpm.ticket_compliance import closes_refs

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer")

CORRECTNESS_ONLY_SCOPE = "correctness findings only (documentation/tests change)"

# Rules a docs/tests-only review drops: shape and style findings, not
# correctness (#1172).
NON_CORRECTNESS_RULES = frozenset({
    "dead-code", "complexity-hotspot", "high-complexity", "type-safety-gap",
})

# Defensive model rules eligible for the reachable-caller post-filter. Security
# rules and deterministic linter rule IDs are intentionally absent.
DEFENSIVE_RULES = frozenset({
    "missing-error-handling", "missing-timeout", "missing-pagination",
    "missing-validation", "unchecked-input", "null-deref",
    "off-by-one-or-bounds", "type-safety-gap", "subprocess-no-timeout",
    "hot-path-unguarded",
})
REACHABLE_SOURCE_RE = re.compile(
    r"\b(?:caller|input)\s+(?:`[^`]+`|[A-Za-z_][\w.]*)", re.IGNORECASE,
)

Grade = Callable[[CodeReviewEvaluation], tuple[FindingJudgement, ...]]
Verify = Callable[[CodeReviewEvaluation], CodeReviewEvaluation]
# (findings, plain_findings, plain_verdicts) -> surviving findings. The plain
# judge's view lets an outage soften a high it already doubted (#1400).
Falsify = Callable[
    [tuple[Finding, ...], tuple[Finding, ...], tuple[FindingJudgement, ...]],
    tuple[Finding, ...],
]


def drop_non_correctness_findings(
    evaluation: CodeReviewEvaluation,
) -> CodeReviewEvaluation:
    """Keep only correctness findings for a correctness-only review."""
    return with_findings(
        evaluation,
        tuple(
            finding for finding in evaluation.findings
            if finding.rule_name not in NON_CORRECTNESS_RULES
        ),
    )


def drop_unreachable_defensive_findings(
    evaluation: CodeReviewEvaluation,
) -> tuple[CodeReviewEvaluation, int]:
    """Drop low/medium defensive findings that name no reachable source."""
    dropped = tuple(
        f for f in evaluation.findings
        if f.rule_name in DEFENSIVE_RULES
        and f.severity in ("low", "medium")
        and not REACHABLE_SOURCE_RE.search(f.message)
    )
    if not dropped:
        return evaluation, 0
    removed = {id(f) for f in dropped}
    return with_findings(
        evaluation, tuple(f for f in evaluation.findings if id(f) not in removed),
    ), len(dropped)


def filter_checked_shell_commands(
    evaluation: CodeReviewEvaluation, file_contents: Mapping[str, str], *,
    installation_id: int = 0, pr: str = "",
) -> CodeReviewEvaluation:
    """Drop `missing-error-handling` findings on shell commands whose exit
    status control flow or errexit already checks (#771)."""
    kept, dropped = filter_checked_shell_findings(
        evaluation.findings, file_contents,
    )
    if not dropped:
        return evaluation
    log.info(
        "code_review_shell_flow_filter",
        extra={"installation_id": installation_id, "pr": pr, "dropped": dropped},
    )
    return with_findings(evaluation, kept)


def filter_intent_mismatches(
    evaluation: CodeReviewEvaluation, pr_context: Mapping[str, Any], *,
    installation_id: int = 0, pr: str = "",
) -> CodeReviewEvaluation:
    """Apply the deterministic intent-mismatch evidence filter (#1170), then
    scope what is left to the boxes a Refs-only PR claims (#1218)."""
    kept, dropped, lowered = filter_intent_mismatch_findings(evaluation.findings)
    body = str(pr_context.get("body") or "")
    kept, scope_dropped, scope_lowered = scope_intent_mismatch_to_claimed_boxes(
        kept,
        boxes=rendered_criteria_with_state(
            str(pr_context.get("linked_issue_context") or ""),
        ),
        closed_issues=closes_refs(body),
        pr_body=body,
    )
    if kept != evaluation.findings:
        evaluation = with_findings(evaluation, kept)
    if dropped or lowered or scope_dropped or scope_lowered:
        log.info(
            "code_review_intent_filter",
            extra={
                "installation_id": installation_id,
                "pr": pr,
                "dropped": dropped,
                "lowered": lowered,
                "scope_dropped": scope_dropped,
                "scope_lowered": scope_lowered,
            },
        )
    return evaluation


@dataclass(frozen=True)
class FilteredFindings:
    evaluation: CodeReviewEvaluation
    defensive_dropped: int


def filter_findings(
    evaluation: CodeReviewEvaluation, file_contents: Mapping[str, str],
    pr_context: Mapping[str, Any], *, installation_id: int = 0, pr: str = "",
) -> FilteredFindings:
    """The deterministic filters, in the funnel's fixed order. Only the
    correctness-only scope drops non-correctness rules: the quarantined
    pass's security scope has its own merge rule (#1569)."""
    if pr_context.get("review_scope") == CORRECTNESS_ONLY_SCOPE:
        evaluation = drop_non_correctness_findings(evaluation)
    evaluation, defensive_dropped = drop_unreachable_defensive_findings(evaluation)
    evaluation = filter_checked_shell_commands(
        evaluation, file_contents, installation_id=installation_id, pr=pr,
    )
    evaluation = filter_intent_mismatches(
        evaluation, pr_context, installation_id=installation_id, pr=pr,
    )
    return FilteredFindings(evaluation, defensive_dropped)


@dataclass(frozen=True)
class FunnelResult:
    """What publishes, plus what the judge saw. `graded` and `verdicts` keep
    the full judged set so eval denominators count suppressed rows too."""

    evaluation: CodeReviewEvaluation
    graded: tuple[Finding, ...]
    verdicts: tuple[FindingJudgement, ...]
    suppressed: tuple[Finding, ...]
    defensive_dropped: int


def publish_funnel(
    evaluation: CodeReviewEvaluation, file_contents: Mapping[str, str],
    pr_context: Mapping[str, Any], *, grade: Grade, verify: Verify,
    falsify: Falsify, installation_id: int = 0, pr: str = "",
) -> FunnelResult:
    """Run every filter and gate, in order, on one pass's evaluation."""
    filtered = filter_findings(
        evaluation, file_contents, pr_context,
        installation_id=installation_id, pr=pr,
    )
    evaluation = filtered.evaluation
    graded = evaluation.findings
    verdicts = tuple(grade(evaluation))
    kept, suppressed = partition_findings(graded, verdicts)
    if suppressed:
        evaluation = with_findings(evaluation, kept)
    evaluation = verify(evaluation)
    surviving = falsify(evaluation.findings, graded, verdicts)
    if surviving != evaluation.findings:
        evaluation = with_findings(evaluation, surviving)
    return FunnelResult(
        evaluation, graded, verdicts, tuple(suppressed), filtered.defensive_dropped,
    )
