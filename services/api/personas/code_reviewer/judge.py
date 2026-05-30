# MIRRORED — sibling at services/webhook/personas/code_reviewer/judge.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""LLM-as-a-judge orchestration for the Elder persona (#190).

Runs AFTER a review is published: grades each surviving finding via a
second LLM call (`llm_client.judge_findings`) and submits a per-finding
`is_real_bug` evaluation to DD LLM Obs, attached to the original review
span. Together with the reaction-poll pipeline (#190b) this builds a
ground-truth dataset for prompt optimization.

Best-effort contract: the developer already saw the review before this
runs. A judge failure — LLM down, parse error, DD submit error — must
never raise, never alter the review outcome. Every exit path swallows.

The judge is gated on two preconditions, both meaning "nothing useful
to record":
  - no findings (clean review — nothing to grade)
  - no review_span_context (review degraded or span export failed —
    nowhere to attach the eval)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from llm_client import (
    FindingJudgement,
    JudgeFindingRepr,
    PrContext,
    judge_findings,
    submit_finding_evaluation,
)
from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.judge")


def _finding_to_repr(f: Finding) -> JudgeFindingRepr:
    """Persona Finding → primitive `JudgeFindingRepr` for the judge LLM
    call. `llm_client` is a lower layer and must not import the persona
    `Finding`; the TypedDict (defined down there) is the typed boundary,
    so a key typo here fails type-check rather than degrading to `?` in
    the judge prompt."""
    return {
        "rule_name": f.rule_name,
        "file": f.file,
        "line": f.line,
        "severity": f.severity,
        "message": f.message,
    }


def _eval_tags(f: Finding) -> dict[str, str]:
    """DD evaluation tags — finding identity for the annotation-queue
    UI to group + filter. `line` is stringified so all tag values are
    str (DD infers facet type from the first value seen)."""
    return {
        "rule_name": f.rule_name,
        "file": f.file,
        "line": str(f.line),
        "severity": f.severity,
    }


def run_judge(
    evaluation: CodeReviewEvaluation,
    hunks: tuple[DiffHunk, ...],
    installation_id: int,
    *,
    review_span_context: Optional[dict],
    pr_context: Optional[PrContext] = None,
) -> None:
    """Grade the evaluation's findings and submit DD LLM Obs evals.

    Wraps the whole body so a judge failure can't disturb the
    already-published review (the load-bearing best-effort contract;
    gating + no-op rationale in the module docstring).
    """
    if not evaluation.findings:
        return
    if review_span_context is None:
        # Review degraded or span export failed — an eval with nowhere
        # to attach would be rejected by DD. Skip rather than burn an
        # LLM call whose result can't be recorded.
        log.info(
            "judge_skipped_no_review_span",
            extra={"findings": len(evaluation.findings)},
        )
        return

    try:
        findings = evaluation.findings
        verdicts = judge_findings(
            [_finding_to_repr(f) for f in findings],
            list(hunks),
            installation_id=installation_id,
            pr_context=pr_context,
        )
        # Map verdict.finding_index → finding. A verdict whose index is
        # out of range (hallucinated) or absent is dropped — only
        # findings the judge actually graded get an eval. Dedupe on
        # index (first verdict wins): a misbehaving judge that emits two
        # verdicts for one finding would otherwise submit two evals for
        # it, skewing the ground-truth dataset toward that finding.
        seen_indices: set[int] = set()
        for v in verdicts:
            if not (0 <= v.finding_index < len(findings)):
                continue
            if v.finding_index in seen_indices:
                continue
            seen_indices.add(v.finding_index)
            f = findings[v.finding_index]
            submit_finding_evaluation(
                is_real_bug=v.is_real_bug,
                reasoning=v.reasoning,
                review_span_context=review_span_context,
                tags=_eval_tags(f),
            )
        log.info(
            "judge_completed",
            extra={
                "findings": len(findings),
                "verdicts": len(verdicts),
                "real_bugs": sum(1 for v in verdicts if v.is_real_bug),
            },
        )
    except Exception as e:  # noqa: BLE001 — best-effort, never disturb the review
        log.error(
            "judge_unhandled",
            extra={"kind": type(e).__name__},
            exc_info=True,
        )
