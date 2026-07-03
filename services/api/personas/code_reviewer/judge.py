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
    Hunk,
    JudgeFindingRepr,
    PrContext,
    judge_findings,
    submit_finding_evaluation,
)
from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
from personas.code_reviewer.sast import EXPOSED_SECRET
from review_types import Severity

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.judge")

# Confidence at/above which a not-real verdict on a low/medium finding
# suppresses publication (#467, ADR-0011). A single global floor; per-repo
# thresholds learned from reactions are #361.
_JUDGE_CONFIDENCE_FLOOR = 0.7

# Only these severities are ever judge-suppressible - a high/critical finding
# ALWAYS publishes, so a judge FP on a critical can never hide it (#346).
_SUPPRESSIBLE_SEVERITIES: frozenset[Severity] = frozenset(("low", "medium"))


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


def eval_tags(f: Finding) -> dict[str, str]:
    """DD evaluation tags — finding identity for the annotation-queue
    UI to group + filter. `line` is stringified so all tag values are
    str (DD infers facet type from the first value seen)."""
    return {
        "rule_name": f.rule_name,
        "file": f.file,
        "line": str(f.line),
        "severity": f.severity,
    }


def grade_findings(
    evaluation: CodeReviewEvaluation,
    hunks: tuple[DiffHunk, ...],
    installation_id: int,
    *,
    pr_context: Optional[PrContext] = None,
    file_contents: Optional[dict[str, str]] = None,
) -> tuple[FindingJudgement, ...]:
    """Run the judge LLM over the evaluation's findings and return its
    verdicts (#467). Fail-OPEN: an empty finding set, an over-budget PR, or
    any LLM/parse error returns `()` so the caller suppresses nothing. This
    is the ONLY function that makes the judge LLM call; both the
    publication gate (`partition_findings`) and the DD evals (`submit_evals`)
    consume its result, so a review makes exactly one judge call. The
    `_JUDGE_MAX_FINDINGS` cost guard lives inside `judge_findings` (it returns
    `()` above the cap), so a firehose PR fail-opens there - no duplicate
    check here."""
    if not evaluation.findings:
        return ()
    try:
        # Convert parser DiffHunks → wire `Hunk`s (path/body) the SAME way the
        # review path does (dispatch._to_llm_hunks). judge_findings is typed
        # `list[Hunk]` and reads `.path`; passing raw DiffHunks (field is
        # `file_path`) crashed the judge with AttributeError on EVERY review
        # with findings, silently killing all `is_real_bug` LLM-Obs evals.
        verdicts = judge_findings(
            [_finding_to_repr(f) for f in evaluation.findings],
            [Hunk(path=h.file_path, body=h.body) for h in hunks],
            installation_id=installation_id,
            pr_context=pr_context,
            file_contents=file_contents,
        )
        log.info(
            "judge_completed",
            extra={
                "findings": len(evaluation.findings),
                "verdicts": len(verdicts),
                "real_bugs": sum(1 for v in verdicts if v.is_real_bug),
            },
        )
        return verdicts
    except Exception as e:  # noqa: BLE001 — fail-open: grade nothing, publish all
        log.error("judge_grade_failed", extra={"kind": type(e).__name__}, exc_info=True)
        return ()


def partition_findings(
    findings: tuple[Finding, ...],
    verdicts: tuple[FindingJudgement, ...],
    *,
    confidence_floor: float = _JUDGE_CONFIDENCE_FLOOR,
) -> tuple[tuple[Finding, ...], tuple[Finding, ...]]:
    """Split findings into (KEPT, SUPPRESSED) on the judge verdicts (#467).

    A finding is SUPPRESSED iff ALL hold: the judge graded it, called it
    not-real, with `confidence >= confidence_floor`, AND its severity is
    low/medium. HIGH/CRITICAL always publish (a judge FP on a critical must
    never hide it, #346). A finding with no verdict (judge outage,
    hallucinated index, over budget) is KEPT - fail-open. Pure - no IO."""
    # First verdict per index wins (a misbehaving judge emitting two verdicts
    # for one finding must not get a second vote).
    by_index: dict[int, FindingJudgement] = {}
    for v in verdicts:
        if 0 <= v.finding_index < len(findings) and v.finding_index not in by_index:
            by_index[v.finding_index] = v

    kept: list[Finding] = []
    suppressed: list[Finding] = []
    for i, f in enumerate(findings):
        v = by_index.get(i)
        if (
            v is not None
            and not v.is_real_bug
            and v.confidence >= confidence_floor
            and f.severity in _SUPPRESSIBLE_SEVERITIES
        ):
            suppressed.append(f)
        else:
            kept.append(f)
    return tuple(kept), tuple(suppressed)


def submit_evals(
    findings: tuple[Finding, ...],
    verdicts: tuple[FindingJudgement, ...],
    *,
    review_span_context: Optional[dict],
) -> None:
    """Submit one DD LLM-Obs `is_real_bug` eval per graded finding (#190),
    attached to the review span. Called for ALL findings - kept AND
    suppressed (#467) - so the precision-metric denominator and the
    learning corpus keep every judged row. Best-effort: never raises."""
    if not findings or review_span_context is None:
        # No span -> nowhere to attach; DD would reject the eval. Skip.
        if findings and review_span_context is None:
            log.info("judge_evals_skipped_no_review_span", extra={"findings": len(findings)})
        return
    try:
        # Dedupe on index (first verdict wins) - a double verdict for one
        # finding would otherwise submit two evals, skewing the dataset.
        seen_indices: set[int] = set()
        for v in verdicts:
            if not (0 <= v.finding_index < len(findings)):
                continue
            if v.finding_index in seen_indices:
                continue
            seen_indices.add(v.finding_index)
            f = findings[v.finding_index]
            # An exposed-secret finding's judge reasoning is generated from the
            # full raw file content (#336) and can quote the credential; never
            # ship that free text to DD. The is_real_bug label + tags (the
            # ground-truth signal this dataset is for) are still recorded.
            reasoning = (
                "[redacted: exposed-secret]"
                if f.rule_name == EXPOSED_SECRET
                else v.reasoning
            )
            submit_finding_evaluation(
                is_real_bug=v.is_real_bug,
                reasoning=reasoning,
                review_span_context=review_span_context,
                tags=eval_tags(f),
            )
    except Exception as e:  # noqa: BLE001 — best-effort, never disturb the review
        log.error("judge_submit_failed", extra={"kind": type(e).__name__}, exc_info=True)


def run_judge(
    evaluation: CodeReviewEvaluation,
    hunks: tuple[DiffHunk, ...],
    installation_id: int,
    *,
    review_span_context: Optional[dict],
    pr_context: Optional[PrContext] = None,
    file_contents: Optional[dict[str, str]] = None,
) -> None:
    """Grade the evaluation's findings and submit DD LLM Obs evals - the
    eval-only compose (`grade_findings` + `submit_evals`), NO publication
    filtering. Retained for callers/tests that only want to record evals;
    the judge-gated publish path (dispatch, #467) calls the primitives
    directly so it can filter between them. Best-effort - never raises."""
    if review_span_context is None:
        if evaluation.findings:
            log.info(
                "judge_skipped_no_review_span",
                extra={"findings": len(evaluation.findings)},
            )
        return
    verdicts = grade_findings(
        evaluation, hunks, installation_id,
        pr_context=pr_context, file_contents=file_contents,
    )
    submit_evals(
        evaluation.findings, verdicts, review_span_context=review_span_context,
    )
