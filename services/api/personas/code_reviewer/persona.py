# MIRRORED — sibling at services/webhook/personas/code_reviewer/persona.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
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
    """

    file: str
    line: int
    severity: Severity
    rule_name: str
    message: str
    suggestion: str


@dataclass(frozen=True, slots=True)
class CodeReviewEvaluation:
    """Aggregate verdict from one Elder review pass.

    `conclusion` follows `CheckConclusion` (spec 0001) so this dataclass
    composes 1:1 into a `CheckRunResult` for GitHub's Checks API.

    `passed` semantics: zero high+critical findings. Medium+low are
    advisory — they appear in `findings` but don't flip `passed`. When
    the LLM call itself failed (`no_diff`, `all_failed`, `parse_failed`
    on the input `LlmReviewResponse`), `passed=True` + `conclusion=neutral`
    keeps the PR un-blocked — Elder is advisory-first.
    """

    findings: tuple[Finding, ...]
    passed: bool
    conclusion: CheckConclusion


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
    if llm_response.kind != "reviewed":
        return CodeReviewEvaluation(
            findings=(), passed=True, conclusion="neutral",
        )

    line_index = _hunk_line_index(hunks)
    kept: list[Finding] = []
    for raw in llm_response.findings:
        allowed_lines = line_index.get(raw.path)
        if allowed_lines is None or raw.line not in allowed_lines:
            # Anti-hallucination: the LLM named a file/line that isn't
            # in the diff. Drop silently — caller can compare
            # `len(llm_response.findings)` vs `len(out.findings)` to
            # surface a metric (next slice's concern, not evaluate's).
            continue
        kept.append(
            Finding(
                file=raw.path,
                line=raw.line,
                severity=raw.severity,
                rule_name=raw.rule,
                message=raw.message,
                # The LLM client's wire-format Finding doesn't carry a
                # `suggestion` field today — leave empty until the
                # prompt is extended in a future slice. Stable field so
                # the inline-comment publisher has a place to read.
                suggestion="",
            )
        )

    blocking = [f for f in kept if f.severity in _BLOCKING_SEVERITIES]
    passed = not blocking
    conclusion: CheckConclusion = "success" if passed else "failure"
    return CodeReviewEvaluation(
        findings=tuple(kept), passed=passed, conclusion=conclusion,
    )
