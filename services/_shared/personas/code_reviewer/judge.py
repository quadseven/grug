"""LLM-as-a-judge orchestration for the Elder persona (#190).

Runs AFTER a review is published: grades each surviving finding via a
second LLM call (`llm_client.judge_findings`) and submits a per-finding
`is_real_bug` evaluation to DD LLM Obs. Ensemble findings fan the verdict
out to every producer span; legacy and deterministic findings fall back
to the response-level review span. Together with the reaction-poll
pipeline (#190b) this builds a ground-truth dataset for prompt optimization.

Best-effort contract: the developer already saw the review before this
runs. A judge failure — LLM down, parse error, DD submit error — must
never raise, never alter the review outcome. Every exit path swallows.

The judge is gated on two preconditions, both meaning "nothing useful
to record":
  - no findings (clean review - nothing to grade)
  - no finding-level or response-level span context (nowhere to attach
    the eval)
"""
from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Optional

from llm_client import (
    BackendConfig,
    FindingJudgement,
    FindingOrigin,
    Hunk,
    JUDGE_BATCH_SIZE,
    JUDGE_MAX_FINDINGS,
    JudgeFindingRepr,
    PrContext,
    _cave_judge_config,
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


def _scope_evidence(
    findings: list[JudgeFindingRepr],
    hunks: list[Hunk],
    file_contents: Optional[dict[str, str]],
) -> tuple[list[Hunk], Optional[dict[str, str]]]:
    """Build the smallest evidence packet that can prove this batch.

    Discovery may inspect an entire review cohort, but adjudication should not
    recreate a whole-PR prompt. Cross-file snippets stay available because
    they were selected upstream as bounded dependency evidence.
    """
    paths = {finding["file"] for finding in findings}
    scoped_hunks = [hunk for hunk in hunks if hunk.path in paths]
    scoped_files = (
        {path: body for path, body in file_contents.items() if path in paths}
        if file_contents is not None
        else None
    )
    return scoped_hunks, scoped_files


def _judge_evidence_packet(
    findings: list[JudgeFindingRepr],
    hunks: list[Hunk],
    installation_id: int,
    *,
    pr_context: Optional[PrContext],
    file_contents: Optional[dict[str, str]],
    cross_file_contents: Optional[dict[str, str]],
    runtime_context: str | None,
    refute: bool = False,
) -> tuple[FindingJudgement, ...]:
    """Use the owned hot reasoner first, then a redacted cloud fallback."""
    cave_config: BackendConfig | None = _cave_judge_config()
    if cave_config is not None:
        try:
            verdicts = judge_findings(
                findings,
                hunks,
                installation_id=installation_id,
                pr_context=pr_context,
                file_contents=file_contents,
                cross_file_contents=cross_file_contents,
                runtime_context=runtime_context,
                config=cave_config,
                redact=False,
                refute=refute,
            )
            expected_indices = set(range(len(findings)))
            actual_indices = {verdict.finding_index for verdict in verdicts}
            if (
                len(verdicts) == len(findings)
                and actual_indices == expected_indices
            ):
                return verdicts
            log.warning(
                "judge_owned_reasoner_incomplete_falling_back",
                extra={
                    "findings": len(findings),
                    "verdicts": len(verdicts),
                    "refute": refute,
                },
            )
        except Exception as e:  # noqa: BLE001 - bounded fallback below
            log.error(
                "judge_owned_reasoner_failed_falling_back",
                extra={
                    "kind": type(e).__name__,
                    "findings": len(findings),
                    "refute": refute,
                },
                exc_info=True,
            )
    return judge_findings(
        findings,
        hunks,
        installation_id=installation_id,
        pr_context=pr_context,
        file_contents=file_contents,
        cross_file_contents=cross_file_contents,
        runtime_context=runtime_context,
        config=None,
        redact=True,
        refute=refute,
    )


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


def eval_tags(
    f: Finding, *, origin: Optional[FindingOrigin] = None,
) -> dict[str, str]:
    """DD evaluation tags — finding identity for the annotation-queue
    UI to group + filter. `line` is stringified so all tag values are
    str (DD infers facet type from the first value seen)."""
    tags = {
        "rule_name": f.rule_name,
        "file": f.file,
        "line": str(f.line),
        "severity": f.severity,
    }
    if origin is not None:
        tags["source_backend"] = origin.backend.value
        tags["source_model"] = origin.model
    return tags


def _eval_targets(
    finding: Finding,
    fallback_span_context: Optional[dict],
) -> tuple[tuple[dict, Optional[FindingOrigin]], ...]:
    """Return every exported producer span, or the legacy global span.

    The fallback keeps SAST findings and records created before ensemble
    provenance trainable. An ensemble finding with origins but no exported
    origin span stays unattributed; attaching it to the response-level first
    success would train the wrong backend.
    """
    origin_targets = tuple(
        (origin.review_span_context, origin)
        for origin in finding.origins
        if origin.review_span_context is not None
    )
    if origin_targets:
        return origin_targets
    if finding.origins:
        return ()
    if fallback_span_context is not None:
        return ((fallback_span_context, None),)
    return ()


def grade_findings(
    evaluation: CodeReviewEvaluation,
    hunks: tuple[DiffHunk, ...],
    installation_id: int,
    *,
    pr_context: Optional[PrContext] = None,
    file_contents: Optional[dict[str, str]] = None,
    cross_file_contents: Optional[dict[str, str]] = None,
    runtime_context: str | None = None,
) -> tuple[FindingJudgement, ...]:
    """Run the judge LLM over the evaluation's findings and return its
    verdicts (#467). Fail-OPEN: an empty finding set or an LLM/parse error
    returns no verdict for the affected findings, so the caller suppresses
    nothing. This
    is the ONLY function that makes the judge LLM call; both the
    publication gate (`partition_findings`) and the DD evals (`submit_evals`)
    consume its result. Large ensemble outputs are split into bounded batches;
    one failed batch does not discard verdicts for the others."""
    if not evaluation.findings:
        return ()
    # Convert parser DiffHunks -> wire `Hunk`s (path/body) the same way the
    # review path does. Passing raw DiffHunks once crashed every judge call.
    wire_hunks = [Hunk(path=h.file_path, body=h.body) for h in hunks]
    finding_reprs = [_finding_to_repr(f) for f in evaluation.findings]
    verdicts: list[FindingJudgement] = []
    failed_batches = 0
    graded_reprs = finding_reprs[:JUDGE_MAX_FINDINGS]
    ungraded_due_to_cap = len(finding_reprs) - len(graded_reprs)
    if ungraded_due_to_cap:
        log.warning(
            "judge_total_cap_reached",
            extra={
                "findings": len(finding_reprs),
                "max": JUDGE_MAX_FINDINGS,
                "ungraded": ungraded_due_to_cap,
            },
        )
    for start in range(0, len(graded_reprs), JUDGE_BATCH_SIZE):
        batch = graded_reprs[start:start + JUDGE_BATCH_SIZE]
        scoped_hunks, scoped_files = _scope_evidence(
            batch, wire_hunks, file_contents,
        )
        try:
            batch_verdicts = _judge_evidence_packet(
                batch,
                scoped_hunks,
                installation_id=installation_id,
                pr_context=pr_context,
                file_contents=scoped_files,
                cross_file_contents=cross_file_contents,
                runtime_context=runtime_context,
            )
        except Exception as e:  # noqa: BLE001 - fail-open per bounded batch
            failed_batches += 1
            log.error(
                "judge_grade_batch_failed",
                extra={
                    "kind": type(e).__name__,
                    "batch_start": start,
                    "batch_size": len(batch),
                },
                exc_info=True,
            )
            continue
        verdicts.extend(
            replace(verdict, finding_index=verdict.finding_index + start)
            for verdict in batch_verdicts
        )
    result = tuple(verdicts)
    log.info(
        "judge_completed",
        extra={
            "findings": len(evaluation.findings),
            "batches": (len(graded_reprs) + JUDGE_BATCH_SIZE - 1)
            // JUDGE_BATCH_SIZE,
            "failed_batches": failed_batches,
            "ungraded_due_to_cap": ungraded_due_to_cap,
            "verdicts": len(result),
            "real_bugs": sum(1 for v in result if v.is_real_bug),
        },
    )
    return result


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
    """Submit one DD LLM-Obs `is_real_bug` eval per finding origin (#190).

    Called for ALL findings - kept AND suppressed (#467) - so the
    precision-metric denominator and learning corpus keep every judged row.
    Legacy/global span attribution is the fallback. Best-effort: never raises.
    """
    if not findings:
        return
    skipped_no_span = 0
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
        targets = _eval_targets(f, review_span_context)
        if not targets:
            skipped_no_span += 1
            continue
        # An exposed-secret finding's judge reasoning is generated from the
        # full raw file content (#336) and can quote the credential; never
        # ship that free text to DD. The is_real_bug label + tags (the
        # ground-truth signal this dataset is for) are still recorded.
        reasoning = (
            "[redacted: exposed-secret]"
            if f.rule_name == EXPOSED_SECRET
            else v.reasoning
        )
        for span_context, origin in targets:
            try:
                submit_finding_evaluation(
                    is_real_bug=v.is_real_bug,
                    reasoning=reasoning,
                    review_span_context=span_context,
                    tags=eval_tags(f, origin=origin),
                )
            except Exception as e:  # noqa: BLE001 - best-effort per producer span
                log.error(
                    "judge_submit_failed",
                    extra={
                        "kind": type(e).__name__,
                        "source_backend": (
                            origin.backend.value if origin is not None else None
                        ),
                    },
                    exc_info=True,
                )
    if skipped_no_span:
        log.info(
            "judge_evals_skipped_no_review_span",
            extra={"findings": skipped_no_span},
        )


def run_judge(
    evaluation: CodeReviewEvaluation,
    hunks: tuple[DiffHunk, ...],
    installation_id: int,
    *,
    review_span_context: Optional[dict],
    pr_context: Optional[PrContext] = None,
    file_contents: Optional[dict[str, str]] = None,
    cross_file_contents: Optional[dict[str, str]] = None,
    runtime_context: str | None = None,
) -> None:
    """Grade the evaluation's findings and submit DD LLM Obs evals - the
    eval-only compose (`grade_findings` + `submit_evals`), NO publication
    filtering. Retained for callers/tests that only want to record evals;
    the judge-gated publish path (dispatch, #467) calls the primitives
    directly so it can filter between them. Best-effort - never raises."""
    if not any(
        _eval_targets(finding, review_span_context)
        for finding in evaluation.findings
    ):
        if evaluation.findings:
            log.info(
                "judge_skipped_no_review_span",
                extra={"findings": len(evaluation.findings)},
            )
        return
    verdicts = grade_findings(
        evaluation, hunks, installation_id,
        pr_context=pr_context,
        file_contents=file_contents,
        cross_file_contents=cross_file_contents,
        runtime_context=runtime_context,
    )
    submit_evals(
        evaluation.findings, verdicts, review_span_context=review_span_context,
    )


# --- Refute gate (#714, epic #707) ------------------------------------

# Kill bar for the refute gate: deliberately far above the suppression
# floor - an evidence-backed confident refutation is a different epistemic
# standard from a plausibility grade, and it is allowed to kill
# HIGH/CRITICAL findings (narrowing the #467-era always-publish rule for
# exactly this gate).
_REFUTE_CONFIDENCE_FLOOR = 0.9


def refute_findings(
    findings: tuple[Finding, ...],
    hunks: tuple[DiffHunk, ...],
    installation_id: int,
    *,
    pr_context: Optional[PrContext] = None,
    file_contents: Optional[dict[str, str]] = None,
    cross_file_contents: Optional[dict[str, str]] = None,
    runtime_context: str | None = None,
) -> tuple[FindingJudgement, ...]:
    """Adversarial evidence-check (#714) for the HIGH/CRITICAL findings
    that survived the deterministic verification pass. One refute-framed
    judge call (the burden inverts: the adjudicator must ground the claim
    in quoted code or refute it); the semantic-misreading class - two
    same-day production instances (grug PR #710, digital-ledger#208) -
    passed the plausibility judge because nothing forced a line-level
    check of the claim itself. Fail-OPEN like grade_findings: any error
    returns () and everything publishes. Callers pass ONLY the
    high/critical subset (cost bound: typically 0-2 findings)."""
    if not findings:
        return ()
    wire_hunks = [Hunk(path=h.file_path, body=h.body) for h in hunks]
    reprs = [_finding_to_repr(f) for f in findings[:JUDGE_BATCH_SIZE]]
    scoped_hunks, scoped_files = _scope_evidence(
        reprs, wire_hunks, file_contents,
    )
    try:
        return _judge_evidence_packet(
            reprs,
            scoped_hunks,
            installation_id=installation_id,
            pr_context=pr_context,
            file_contents=scoped_files,
            cross_file_contents=cross_file_contents,
            runtime_context=runtime_context,
            refute=True,
        )
    except Exception as e:  # noqa: BLE001 - fail-open, mirror grade_findings
        log.error(
            "refute_gate_failed",
            extra={"kind": type(e).__name__, "count": len(reprs)},
            exc_info=True,
        )
        return ()


def partition_refuted(
    findings: tuple[Finding, ...],
    verdicts: tuple[FindingJudgement, ...],
    *,
    confidence_floor: float = _REFUTE_CONFIDENCE_FLOOR,
) -> tuple[tuple[Finding, ...], tuple[Finding, ...]]:
    """Split (KEPT, REFUTED) on the refute-gate verdicts. A finding is
    REFUTED iff the gate graded it not-real with confidence >= the refute
    floor. No verdict (outage, index miss, over budget) = KEPT - the gate
    only ever kills on decisive quoted-evidence refutation. Pure - no IO."""
    by_index: dict[int, FindingJudgement] = {}
    for v in verdicts:
        if 0 <= v.finding_index < len(findings) and v.finding_index not in by_index:
            by_index[v.finding_index] = v
    kept: list[Finding] = []
    refuted: list[Finding] = []
    for i, f in enumerate(findings):
        v = by_index.get(i)
        if v is not None and not v.is_real_bug and v.confidence >= confidence_floor:
            refuted.append(f)
        else:
            kept.append(f)
    return tuple(kept), tuple(refuted)
