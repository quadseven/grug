"""Blocking comment budget and the drip-feed rule (grug#1570).

A review caps its blocking markings per PR and holds the rest back from inline
comments. Held findings stay in the evaluation, so the check still fails on
them, and they appear ranked in the one board comment. On a second or later
round, a new finding on an unchanged line drops unless it is critical: nobody
reads round two of a drip feed.

Only model findings are in scope. A finding with no model origin came from a
deterministic scanner and always posts.
"""
from __future__ import annotations

from collections.abc import Mapping

from dataclasses import dataclass

from personas.code_reviewer.dedup import finding_key
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.quarantine import switch_on
from personas.code_reviewer.repo_config import _SEVERITY_RANK

# Judge view by finding key: (called it real, confidence).
JudgeConfidence = Mapping[str, tuple[bool, float]]

_BLOCKING = frozenset({"high", "critical"})
_TOP_TIER = "critical"
# A finding the judge never graded ranks between its doubted and trusted peers.
_UNGRADED_CONFIDENCE = 0.5


def budget_enabled() -> bool:
    """`GRUG_COMMENT_BUDGET`, off unless set."""
    return switch_on("GRUG_COMMENT_BUDGET")


def _key(finding: Finding) -> str:
    return finding_key(finding.file, finding.line, finding.rule_name)


def is_model_blocking(finding: Finding) -> bool:
    return bool(finding.origins) and finding.severity in _BLOCKING


def _rank(finding: Finding, confidence: JudgeConfidence) -> float:
    """Severity x confidence, higher first."""
    judged = confidence.get(_key(finding))
    score = judged[1] if judged else _UNGRADED_CONFIDENCE
    return (_SEVERITY_RANK[finding.severity] + 1) * score


@dataclass(frozen=True)
class BudgetSplit:
    inline: tuple[Finding, ...]
    held: tuple[Finding, ...]


def split_for_budget(
    findings: tuple[Finding, ...],
    *,
    cap: int,
    open_blocking: int,
    confidence: JudgeConfidence,
) -> BudgetSplit:
    """Split new findings into inline and held. `open_blocking` counts the
    blocking markings earlier rounds already posted. A cap of 0 means no cap.
    Non-blocking findings the judge doubts are held at any cap."""
    doubted = {
        id(f) for f in findings
        if f.origins and f.severity not in _BLOCKING
        and not confidence.get(_key(f), (True, 0.0))[0]
    }
    held = {id(f) for f in findings if id(f) in doubted}
    if cap > 0:
        blocking = sorted(
            (f for f in findings if is_model_blocking(f)),
            key=lambda f: _rank(f, confidence), reverse=True,
        )
        held.update(id(f) for f in blocking[max(0, cap - open_blocking):])
    return BudgetSplit(
        tuple(f for f in findings if id(f) not in held),
        tuple(f for f in findings if id(f) in held),
    )


def drip_fed(
    findings: tuple[Finding, ...],
    *,
    prior_keys: frozenset[str],
    changed_lines: frozenset[tuple[str, int]] | None,
) -> tuple[tuple[Finding, ...], tuple[Finding, ...]]:
    """(kept, dropped) for a round after the first. `changed_lines` holds the
    lines this round changed: empty on a re-review of the same head, None when
    the delta is unknown, which drops nothing."""
    if changed_lines is None:
        return findings, ()
    kept: list[Finding] = []
    dropped: list[Finding] = []
    for finding in findings:
        late = (
            bool(finding.origins)
            and finding.severity != _TOP_TIER
            and _key(finding) not in prior_keys
            and (finding.file, finding.line) not in changed_lines
        )
        (dropped if late else kept).append(finding)
    return tuple(kept), tuple(dropped)


def held_lines(held: tuple[Finding, ...], confidence: JudgeConfidence) -> str:
    """The one ranked list of held findings, or ''."""
    if not held:
        return ""
    ranked = sorted(held, key=lambda f: _rank(f, confidence), reverse=True)
    rows = [f"- {f.severity} `{f.rule_name}` at {f.file}:{f.line}" for f in ranked]
    return "\n".join([
        f"**Held back by the comment budget** ({len(held)}, no inline comment; "
        "the check still counts them):",
        *rows,
    ])
