# MIRRORED — sibling at services/api/personas/code_reviewer/persona.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
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

from dataclasses import dataclass
from typing import Literal

from github_checks_client import CheckConclusion
from llm_client import LlmReviewResponse
from personas.code_reviewer.diff_parser import DiffHunk

Severity = Literal["low", "medium", "high", "critical"]

# A finding at one of these severities flips the aggregate verdict.
# Medium + low remain advisory: reported in the check-run summary but
# `passed=True` so the PR isn't blocked. Mirrors TPM's advisory split
# (issue-link rule fails without blocking).
_BLOCKING_SEVERITIES: frozenset[Severity] = frozenset(("high", "critical"))


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
      - At least one high+critical finding: `conclusion=failure`,
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
    not `"reviewed"` (`no_diff`, `all_failed`, `parse_failed`). All three
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


def _hunk_line_index(hunks: tuple[DiffHunk, ...]) -> dict[str, frozenset[int]]:
    """Build {file_path: union(new_lines)} for O(1) hallucination check."""
    index: dict[str, set[int]] = {}
    for h in hunks:
        index.setdefault(h.file_path, set()).update(h.new_lines)
    return {k: frozenset(v) for k, v in index.items()}


def evaluate_diff(
    hunks: tuple[DiffHunk, ...], llm_response: LlmReviewResponse,
) -> CodeReviewEvaluation:
    """Pure: build a `CodeReviewEvaluation` from hunks + LLM output.

    No IO, no logging side-effects. Spec 0015 attests purity.
    """
    # LLM did not return reviewable content — advisory neutral.
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
        kept.append(
            Finding(
                file=raw.path,
                line=raw.line,
                severity=raw.severity,
                rule_name=raw.rule,
                message=raw.message,
                # The LLM client's wire-format Finding doesn't carry a
                # `suggestion` field today. `None` (not "") so consumers
                # don't conflate "absent" with "empty hint".
                suggestion=None,
            )
        )

    blocking = [f for f in kept if f.severity in _BLOCKING_SEVERITIES]
    conclusion: CheckConclusion = "failure" if blocking else "success"
    return CodeReviewEvaluation(
        findings=tuple(kept),
        conclusion=conclusion,
        dropped_hallucinations=dropped,
    )
