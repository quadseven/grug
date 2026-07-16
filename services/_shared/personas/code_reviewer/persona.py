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

from github_checks_client import CheckConclusion
from llm_client import FindingOrigin, LlmReviewResponse
from personas.code_reviewer.diff_parser import DiffHunk
from review_types import Effort, Severity  # single source (#250)

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
    r"^\s*(?:[A-Za-z0-9_.\/-]+|\"[^\"]+\")\s*[:=]\s*"
    r"(?:[A-Za-z0-9_.\/-]+|['\"][^$'{\"}]*['\"])\s*,?\s*$"
)


def _changed_line_text(hunk: DiffHunk, target_line: int) -> str | None:
    """Return target new-side text from one unified-diff hunk."""
    new_line = hunk.new_start
    for raw in hunk.body.splitlines()[1:]:
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
    # Producer attribution for ensemble findings. Excluded from equality so
    # finding identity remains (file, line, rule) rather than observability
    # metadata; dedup and publication behavior must not change with tracing.
    origins: tuple[FindingOrigin, ...] = field(
        default_factory=tuple, compare=False, repr=False,
    )

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
    contain no usable findings. All degraded states
    map to `conclusion="neutral"` but the cause is preserved so caller
    metrics can distinguish empty PR vs LLM provider outage.
    """

    findings: tuple[Finding, ...]
    conclusion: CheckConclusion
    dropped_hallucinations: int = 0
    degraded_reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.conclusion != "failure"


def with_extra_findings(
    evaluation: CodeReviewEvaluation, extra: tuple[Finding, ...]
) -> CodeReviewEvaluation:
    """Merge additional findings (e.g. SAST candidates the exploitability judge
    KEPT, #400) into an evaluation and re-derive `conclusion` by the SAME rule
    `evaluate_diff` uses: any high/critical -> failure; else preserve a degraded
    `neutral`; else success. Pure — no IO. The `extra` are already diff-anchored
    (built from real hunk line numbers), so they need no re-filtering. Returning
    a new value (frozen dataclass) keeps the merge honest + testable; `passed`
    re-derives from the new conclusion automatically."""
    if not extra:
        return evaluation
    combined = evaluation.findings + extra
    if any(f.severity in _BLOCKING_SEVERITIES for f in combined):
        conclusion: CheckConclusion = "failure"
    elif evaluation.degraded_reason is not None:
        conclusion = "neutral"
    else:
        conclusion = "success"
    return CodeReviewEvaluation(
        findings=combined,
        conclusion=conclusion,
        dropped_hallucinations=evaluation.dropped_hallucinations,
        degraded_reason=evaluation.degraded_reason,
    )


def with_findings(
    evaluation: CodeReviewEvaluation, findings: tuple[Finding, ...]
) -> CodeReviewEvaluation:
    """Replace an evaluation's findings and re-derive `conclusion` by the
    SAME rule as `with_extra_findings` / `evaluate_diff`. Used by the
    judge-gated publish path (#467) to publish only the KEPT findings.
    Suppression only ever removes advisory-severity findings, so the
    conclusion is provably unchanged - but re-deriving keeps the invariant
    honest rather than relying on it. Pure - no IO."""
    if any(f.severity in _BLOCKING_SEVERITIES for f in findings):
        conclusion: CheckConclusion = "failure"
    elif evaluation.degraded_reason is not None:
        conclusion = "neutral"
    else:
        conclusion = "success"
    return CodeReviewEvaluation(
        findings=findings,
        conclusion=conclusion,
        dropped_hallucinations=evaluation.dropped_hallucinations,
        degraded_reason=evaluation.degraded_reason,
    )


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
        )

    line_index = _hunk_line_index(hunks)
    kept: list[Finding] = []
    dropped = 0
    for raw in llm_response.findings:
        allowed_lines = line_index.get(raw.path)
        if allowed_lines is None or raw.line not in allowed_lines:
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
        kept.append(
            Finding(
                file=raw.path,
                line=raw.line,
                severity=raw.severity,
                rule_name=raw.rule,
                message=raw.message,
                # #553: wire fields carry through; the anti-hallucination
                # line gate above already guarantees a suggestion only
                # anchors on a line the LLM actually saw.
                suggestion=raw.suggestion,
                effort=raw.effort,
                origins=raw.origins,
            )
        )

    has_blocking = any(f.severity in _BLOCKING_SEVERITIES for f in kept)
    conclusion: CheckConclusion = "failure" if has_blocking else "success"
    return CodeReviewEvaluation(
        findings=tuple(kept),
        conclusion=conclusion,
        dropped_hallucinations=dropped,
        degraded_reason=None,
    )
