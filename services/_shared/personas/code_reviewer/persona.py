"""Elder (code-reviewer) persona — pure evaluate_diff rollup.

Per spec 0015 §Evaluate contract: `evaluate_diff(hunks, llm_response)`
is pure. Consumes structured hunks from `diff_parser` + the LLM client's
discriminated `LlmReviewResponse` and produces a `CodeReviewEvaluation`
that composes 1:1 into a `CheckRunResult` (spec 0001).

Two load-bearing safety properties:
  1. **Anti-hallucination**: findings whose `(file, line)` is not inside
     any hunk's `new_lines` set are dropped. An LLM-invented line is
     worse than no finding because it teaches developers to ignore
     Elder.
  2. **Advisory degradation**: `kind in {no_diff, all_failed, parse_failed}`
     never blocks. Elder is advisory-first; LLM outages must not 500
     the gate. `conclusion=neutral` so the future blocking flip
     doesn't accidentally fail PRs on infrastructure flakiness.

The persona-level `Finding` has a distinct shape from `llm_client.Finding`
(the wire format): the LLM speaks `path` + `rule`; the persona renames
to `file` + `rule_name` and adds `suggestion` so the GH inline comment
publisher (next slice) has a stable place to read the fix hint.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import get_args

from code_review_prompt import COMPLEXITY_CORRECTNESS_MARKER
from github_checks_client import CheckConclusion
from llm_client import FindingOrigin, LlmReviewResponse, is_withdrawn_finding
from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.line_refs import align_line_refs, repair_where_refs
from review_types import Effort, Severity  # single source (#250)
from review_pipeline import ReviewCoverage

# A finding at one of these severities flips the aggregate verdict.
# Medium + low remain advisory: reported in the check-run summary but
# `passed=True` so the PR isn't blocked. Mirrors TPM's advisory split
# (issue-link rule fails without blocking).
#
# The assert ties the blocking set to the Literal — adding a level to
# `Severity` (e.g. `"blocker"`) without also classifying it as blocking
# or advisory will fail at import time, not silently default to advisory.
_BLOCKING_SEVERITIES: frozenset[Severity] = frozenset(("high", "critical"))
_ADVISORY_SEVERITIES: frozenset[Severity] = frozenset(("low", "medium"))
assert frozenset(get_args(Severity)) == _BLOCKING_SEVERITIES | _ADVISORY_SEVERITIES, (
    "Severity Literal drifted from blocking/advisory partition"
)

_DECLARATIVE_PATH_RE = re.compile(r"\.(?:ya?ml|json|toml)$", re.IGNORECASE)
_STATIC_SCALAR_RE = re.compile(
    r"^\s*(?:[A-Za-z0-9_.\/-]+|\"[^$'\"{}]+\")\s*[:=]\s*"
    r"(?:[A-Za-z0-9_.\/-]+|['\"][^$'{\"}]*['\"])\s*,?\s*$"
)


def _changed_line_text(hunk: DiffHunk, target_line: int) -> str | None:
    """Return target new-side text from one unified-diff hunk."""
    # DiffHunk.body is typed str and validated at parse time, but callers may
    # pass synthetic hunks in tests; never AttributeError on empty/missing body.
    body = getattr(hunk, "body", None)
    if not body:
        return None
    new_line = hunk.new_start
    for raw in body.splitlines()[1:]:
        if raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        text = raw[1:] if raw.startswith(("+", " ")) else raw
        if new_line == target_line:
            return text
        new_line += 1
    return None


def _is_static_declarative_scalar(
    path: str, line: int, hunks: tuple[DiffHunk, ...],
) -> bool:
    """True for literal manifest values with no template/input expression."""
    if _DECLARATIVE_PATH_RE.search(path) is None:
        return False
    for hunk in hunks:
        if hunk.file_path != path or line not in hunk.new_lines:
            continue
        text = _changed_line_text(hunk, line)
        return text is not None and _STATIC_SCALAR_RE.fullmatch(text) is not None
    return False


def _snippet_has_line(snippet: str, line: int) -> bool:
    """Check for an original line number in the bounded cross-file snippet."""
    return any(
        raw.partition(":")[0].strip().isdigit()
        and int(raw.partition(":")[0].strip()) == line
        for raw in snippet.splitlines()
    )


@dataclass(frozen=True, slots=True)
class Budget:
    """A measured limit a finding is over (grug#1429): the current value,
    the limit it must meet, and the unit both are in. The review renders it
    as the exact number to hit instead of "make it smaller"."""

    metric: str
    current: float
    limit: float
    unit: str = ""


@dataclass(frozen=True, slots=True)
class Finding:
    """Persona-level finding posted as a GitHub inline review comment.

    Distinct from `llm_client.Finding` (the wire-format from the LLM).
    `evaluate_diff` translates from the wire shape and validates that
    `(file, line)` references a line the LLM actually saw.

    `suggestion: str | None` — None means the LLM didn't supply a fix
    hint. Using `None` rather than `""` removes the "empty-vs-absent"
    ambiguity before any consumer ships against it.

    Invariant: `line >= 1` (GitHub's inline-comment API rejects line=0
    with a 422). Checked in `__post_init__` so a malformed wire-format
    finding fails loudly at parse time, not at the GH POST.
    """

    file: str
    line: int
    severity: Severity
    rule_name: str
    message: str
    suggestion: str | None
    # #553: closed-enum fix-effort hint, typed against the shared Literal
    # (review_types.Effort) so an off-vocabulary value is a type error at
    # the constructor, not a silently dropped chip. None = no estimate.
    effort: Effort | None = None
    # Stable identity for deterministic complexity findings across pushes.
    complexity_function: str | None = None
    # Set only for a validated cross-file snippet anchor. These findings are
    # advisory even when the model requested a higher severity.
    outside_diff_reason: str | None = None
    # Producer attribution for ensemble findings. Excluded from equality so
    # finding identity remains (file, line, rule) rather than observability
    # metadata; dedup and publication behavior must not change with tracing.
    origins: tuple[FindingOrigin, ...] = field(
        default_factory=tuple, compare=False, repr=False,
    )
    # grug#1429: measured limits this finding is over, rendered as current ->
    # limit (budget left). Set by deterministic producers that measured them.
    budgets: tuple[Budget, ...] = field(default=(), compare=False)
    # grug#1429: the check's own output line, quoted verbatim in the review
    # when the finding came from a deterministic check (ruff, opengrep).
    check_output: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        assert self.line >= 1, (
            f"Finding.line must be >= 1 (got {self.line}); "
            "GitHub's inline-comment API rejects line=0"
        )


@dataclass(frozen=True, slots=True)
class CodeReviewEvaluation:
    """Aggregate verdict from one Elder review pass.

    `conclusion` follows `CheckConclusion` (spec 0001) so this dataclass
    composes 1:1 into a `CheckRunResult` for GitHub's Checks API.

    `passed` is **derived** from `conclusion` (`passed = conclusion != "failure"`)
    so the two encodings can't drift. The producer (`evaluate_diff`)
    builds `conclusion` from severity + LLM kind:
      - LLM didn't produce content (`no_diff` / `all_failed` /
        `parse_failed`): `conclusion=neutral`, passed=True. Elder is
        advisory-first — infra flakiness must not block PRs.
      - At least one high or critical finding: `conclusion=failure`,
        passed=False.
      - Otherwise: `conclusion=success`, passed=True. Medium+low
        findings are advisory — reported in the check-run summary but
        don't flip the verdict.

    `dropped_hallucinations` is the count of LLM findings rejected
    because their `(file, line)` was not inside any hunk's `new_lines`.
    Surfacing the count (vs silently dropping) means the dispatch layer
    can emit a metric and tell "100% hallucination" from "no findings"
    — both yield `findings=()` but only one is a real clean PR.

    `degraded_reason` carries the `LlmReviewResponse.kind` value when
    not `"reviewed"` (`no_diff`, `all_failed`, `parse_failed`) - these
    contain no usable findings. It carries `partial_review` when bounded
    cohorts returned a mix of usable and failed results; those findings remain
    publishable, but dispatch forces the check advisory. The cause is preserved
    so caller metrics can distinguish empty PR, partial coverage, and outage.
    """

    findings: tuple[Finding, ...]
    conclusion: CheckConclusion
    dropped_hallucinations: int = 0
    degraded_reason: str | None = None
    coverage: ReviewCoverage | None = None
    # grug#1397: candidates the reviewer itself withdrew (`is_finding: false`
    # or a message ending "No finding."). Never published, never counted
    # toward the verdict, but counted here so recall stays measurable.
    dropped_withdrawn: int = 0
    tier_served: str | None = None

    @property
    def passed(self) -> bool:
        return self.conclusion != "failure"


def _derive_conclusion(
    findings: tuple[Finding, ...], degraded_reason: str | None,
) -> CheckConclusion:
    """The ONE rule every `CodeReviewEvaluation` constructor uses to pick a
    conclusion from findings + coverage: any high/critical finding blocks,
    full stop; short of that, a degraded or partial pass stays `neutral` -
    NEVER `success`, because Elder is vouching only for the ground it
    actually walked; otherwise a genuinely clean pass is `success`.

    Centralised so `evaluate_diff`, `with_extra_findings`, and
    `with_findings` cannot drift into three different answers for the same
    inputs. They used to: `with_extra_findings`/`with_findings` implemented
    this 3-way rule and said in their own docstrings that `evaluate_diff`
    used "the SAME rule" — but `evaluate_diff` only ever checked for
    blocking findings and fell through to `success` whenever there weren't
    any, even when it had JUST set `degraded_reason="partial_review"` three
    lines later. A `CodeReviewEvaluation` could carry
    `conclusion="success"` and `degraded_reason="partial_review"` in the
    SAME instance — self-contradictory, and exactly the shape of grug#813/
    #707's "did not fit one look" pass whose check-run still read `success`.
    """
    if any(f.severity in _BLOCKING_SEVERITIES for f in findings):
        return "failure"
    if degraded_reason is not None:
        return "neutral"
    return "success"


def with_extra_findings(
    evaluation: CodeReviewEvaluation, extra: tuple[Finding, ...]
) -> CodeReviewEvaluation:
    """Merge additional findings (e.g. SAST candidates the exploitability judge
    KEPT, #400) into an evaluation and re-derive `conclusion` via
    `_derive_conclusion`. Pure — no IO. The `extra` are already diff-anchored
    (built from real hunk line numbers), so they need no re-filtering. Returning
    a new value (frozen dataclass) keeps the merge honest + testable; `passed`
    re-derives from the new conclusion automatically."""
    if not extra:
        return evaluation
    combined = evaluation.findings + extra
    return CodeReviewEvaluation(
        findings=combined,
        conclusion=_derive_conclusion(combined, evaluation.degraded_reason),
        dropped_hallucinations=evaluation.dropped_hallucinations,
        dropped_withdrawn=evaluation.dropped_withdrawn,
        degraded_reason=evaluation.degraded_reason,
        coverage=evaluation.coverage,
        tier_served=evaluation.tier_served,
    )


def with_findings(
    evaluation: CodeReviewEvaluation, findings: tuple[Finding, ...]
) -> CodeReviewEvaluation:
    """Replace an evaluation's findings and re-derive `conclusion` via
    `_derive_conclusion` (the SAME rule `with_extra_findings` / `evaluate_diff`
    use). Used by the judge-gated publish path (#467) to publish only the
    KEPT findings. Suppression only ever removes advisory-severity findings,
    so the conclusion is provably unchanged - but re-deriving keeps the
    invariant honest rather than relying on it. Pure - no IO."""
    return CodeReviewEvaluation(
        findings=findings,
        conclusion=_derive_conclusion(findings, evaluation.degraded_reason),
        dropped_hallucinations=evaluation.dropped_hallucinations,
        dropped_withdrawn=evaluation.dropped_withdrawn,
        degraded_reason=evaluation.degraded_reason,
        coverage=evaluation.coverage,
        tier_served=evaluation.tier_served,
    )


def with_degradation(
    evaluation: CodeReviewEvaluation, other: CodeReviewEvaluation,
) -> CodeReviewEvaluation:
    """Fold ANOTHER pass's degradation into this one.

    Elder runs Tier-1 and an async deep (reasoner) arm as two INDEPENDENT
    review passes that each carry their own `degraded_reason`/`coverage`.
    When the deep arm's novel findings are merged on top of Tier-1
    (`with_extra_findings(tier1_eval, novel_deep)`, or a bare `tier1_eval`
    when the deep arm found nothing new), the merge only ever consulted
    `tier1_eval.degraded_reason` — if Tier-1 was clean but the deep arm hit
    partial coverage of its OWN (e.g. #813's budget burned on byte-identical
    content), that fact was silently dropped on the floor: the combined
    evaluation, and the check-run built from it, reported a clean pass over
    ground the deep arm never finished walking.

    `other` here is the pass whose degradation might be missing from
    `evaluation`. If `evaluation` is already degraded, its reason wins
    (first-degradation-wins is arbitrary but stable; both are real). If
    neither is degraded, this is a no-op.

    `coverage` always travels WITH the `degraded_reason` that explains it -
    both taken from `other`, never mixed with `evaluation`'s. `evaluation`
    was clean (that's why this function did anything), so if it separately
    ran its own staged review its `coverage` describes a COMPLETE pass; a
    reader who sees `degraded_reason="partial_review"` next to that number
    would see "3/3 cohorts completed" and reasonably read it as complete
    coverage, when the actual gap - the thing degraded_reason is naming -
    is in `other`'s own, different, cohort plan. Caught in review: an
    earlier version kept `evaluation.coverage` whenever it was non-None,
    which reported the wrong pass's numbers for exactly that case. Pure -
    no IO."""
    if evaluation.degraded_reason or not other.degraded_reason:
        return evaluation
    return CodeReviewEvaluation(
        findings=evaluation.findings,
        conclusion=_derive_conclusion(evaluation.findings, other.degraded_reason),
        dropped_hallucinations=evaluation.dropped_hallucinations,
        dropped_withdrawn=evaluation.dropped_withdrawn,
        degraded_reason=other.degraded_reason,
        coverage=other.coverage,
        tier_served=evaluation.tier_served or other.tier_served,
    )


def _capped_severity(rule: str, severity: Severity, message: str) -> Severity:
    """A complexity finding is advice, so it stays at medium unless its
    message opens with the correctness-risk marker and names a real bug
    risk (grug#1144). Every other kind keeps the severity it came with.
    """
    if rule not in {"high-complexity", "complexity-hotspot"}:
        return severity
    if message.lstrip().lower().startswith(COMPLEXITY_CORRECTNESS_MARKER.lower()):
        return severity
    return "medium"


def _hunk_line_index(hunks: tuple[DiffHunk, ...]) -> dict[str, set[int]]:
    """Build {file_path: union(new_lines)} for O(1) hallucination check.

    Returns plain `set` (not `frozenset`) — the index is built fresh
    per `evaluate_diff` call, never shared or hashed, so the immutable
    wrap was overhead without payoff.
    """
    index: dict[str, set[int]] = {}
    for h in hunks:
        index.setdefault(h.file_path, set()).update(h.new_lines)
    return index


def evaluate_diff(
    hunks: tuple[DiffHunk, ...], llm_response: LlmReviewResponse,
    *, cross_file_contents: dict[str, str] | None = None,
) -> CodeReviewEvaluation:
    """Pure: build a `CodeReviewEvaluation` from hunks + LLM output.

    No IO, no logging side-effects. Spec 0015 attests purity.
    """
    # Preserve `kind` as the degraded_reason so the caller can tell
    # "empty PR" from "every backend failed" (both yield findings=()).
    if llm_response.kind != "reviewed":
        return CodeReviewEvaluation(
            findings=(),
            conclusion="neutral",
            degraded_reason=llm_response.kind,
            coverage=llm_response.coverage,
            tier_served=llm_response.tier_served,
        )

    line_index = _hunk_line_index(hunks)
    kept: list[Finding] = []
    dropped = 0
    withdrawn = 0
    for raw in llm_response.findings:
        # grug#1397: the reviewer ruled this candidate out itself. It must
        # not become a thread or move the verdict; count it and move on.
        if is_withdrawn_finding(raw):
            withdrawn += 1
            continue
        allowed_lines = line_index.get(raw.path)
        outside_diff = allowed_lines is None or raw.line not in allowed_lines
        outside_reason = raw.outside_diff_reason
        if outside_diff and not (
            outside_reason
            and raw.path in (cross_file_contents or {})
            and _snippet_has_line((cross_file_contents or {})[raw.path], raw.line)
        ):
            # Anti-hallucination: the LLM named a file/line that isn't
            # in the diff. Drop + count. Count is surfaced on the
            # evaluation so the dispatch layer can metric and tell
            # "100% hallucination" from "no findings at all" — both
            # yield findings=() but only one is a real clean PR.
            dropped += 1
            continue
        # A repository-owned literal manifest scalar has no external taint
        # source. Reject the exact false-positive class seen on infra#1806;
        # templated values (`{{ }}`, `${...}`) deliberately do not match.
        if (
            raw.rule == "unvalidated-external-input"
            and _is_static_declarative_scalar(raw.path, raw.line, hunks)
        ):
            continue
        # grug#1398: a pre-change number for the anchor line becomes the
        # head-commit number the comment is anchored to; other lines stay.
        message = repair_where_refs(
            align_line_refs(raw.message, raw.path, raw.line, hunks),
            raw.path, raw.line,
        )
        if outside_diff:
            message = f"OUTSIDE DIFF (advisory): {outside_reason} {message}"
        kept.append(
            Finding(
                file=raw.path,
                line=raw.line,
                severity=("medium" if outside_diff else _capped_severity(raw.rule, raw.severity, message)),
                rule_name=raw.rule,
                message=message,
                # #553: wire fields carry through; the anti-hallucination
                # line gate above already guarantees a suggestion only
                # anchors on a line the LLM actually saw.
                suggestion=raw.suggestion,
                effort=raw.effort,
                origins=raw.origins,
                outside_diff_reason=outside_reason if outside_diff else None,
            )
        )

    kept_findings = tuple(kept)
    # Staged review preserves useful findings when another cohort fails (or
    # `max_cohorts` truncated the plan before it ran, #813/#707), but the
    # check must say coverage was partial and stay advisory. The inline
    # findings are still valid, diff-anchored evidence.
    # grug#1097: a short empty answer kept because no other model answered
    # is a clean result, but not a trusted one. Partial coverage outranks it.
    if llm_response.error[:15] == "partial review:":
        degraded_reason: str | None = "partial_review"
    elif llm_response.error[:15] == "low confidence:":
        degraded_reason = "low_confidence_review"
    else:
        degraded_reason = None
    return CodeReviewEvaluation(
        findings=kept_findings,
        # SAME rule with_extra_findings/with_findings use (_derive_conclusion):
        # blocking -> failure; degraded (including partial coverage) ->
        # neutral; else success. `conclusion` and `degraded_reason` must never
        # disagree within one CodeReviewEvaluation - a "success" alongside
        # degraded_reason="partial_review" is the bug this closes.
        conclusion=_derive_conclusion(kept_findings, degraded_reason),
        dropped_hallucinations=dropped,
        dropped_withdrawn=withdrawn,
        degraded_reason=degraded_reason,
        coverage=llm_response.coverage,
        tier_served=llm_response.tier_served,
    )
