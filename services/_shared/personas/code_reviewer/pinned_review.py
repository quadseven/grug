"""Pinned-commit review style for every Elder review body (grug#1429).

THE PROBLEM. An Elder review used to name a problem without the arithmetic
or the closure: "function too tangled" with no number to hit, a check
finding with no quote of what the check said, and no line saying the list
was complete. The author then spends extra rounds learning what would pass.

THE SHAPE. Every review body this module touches:

- starts with the exact head it judged (full sha) and the linked issue(s);
- lists each marking with a concrete target: a measured limit renders as
  current value, the limit and the budget left ("cyclomatic 19 -> at most
  15 (cut 4)"), otherwise the one-line fix;
- quotes a deterministic check's own output line verbatim when the marking
  came from a check;
- lists findings outside the diff as follow-up issues to file, marked
  non-blocking, never as required fixes;
- ends with an explicit closure sentence: either the list is everything
  required, or nothing needs to change. A degraded pass says it cannot
  close, because claiming "nothing else" over unread ground would be false.

This lives in the renderer, not in a prompt: the producers attach the facts
(`Finding.budgets`, `Finding.check_output`) and this module states them the
same way every time. Pure string in, pure string out.
"""

from __future__ import annotations

import re

from personas.code_reviewer.persona import Budget, CodeReviewEvaluation, Finding
from personas.review_publish import is_real_degradation

PIN_PREFIX = "Reviewed head"
CLOSURE_NOTHING = "Nothing needs to change on this head."
CLOSURE_COMPLETE = (
    "The required fix(es) above are everything this head needs; "
    "nothing else needs to change."
)
CLOSURE_ADVISORY_ONLY = (
    "Nothing here blocks this head; the advisory markings are optional and "
    "nothing else needs to change."
)
CLOSURE_DEGRADED = (
    "Grug cannot say nothing else needs to change: this pass did not read "
    "the whole diff. Comment `/grug improve` to review it again."
)

_BLOCKING_SEVERITIES = frozenset({"high", "critical"})
_LIST_CAP = 10
_TARGET_CHARS = 200


def _safe_span(text: str) -> str:
    """A single-line code span that keeps the text verbatim: the fence is one
    backtick longer than the longest run inside, so a check message that
    itself quotes code (ruff's "`os` imported but unused") is not altered."""
    flat = " ".join(str(text).split())
    longest = max((len(run) for run in re.findall(r"`+", flat)), default=0)
    if not longest:
        return f"`{flat}`"
    fence = "`" * (longest + 1)
    return f"{fence} {flat} {fence}"


def _num(value: float) -> str:
    """19 not 19.0; 18.2 stays 18.2."""
    rounded = round(float(value), 2)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:g}"


def budget_text(budget: Budget) -> str:
    """'comment share 18.2% -> at most 15% (cut 3.2%)'.

    The current value, the limit, and how much has to go, so the author knows
    the exact number that passes instead of guessing at "smaller"."""
    unit = budget.unit
    over = max(0.0, float(budget.current) - float(budget.limit))
    return (
        f"{budget.metric} {_num(budget.current)}{unit} -> at most "
        f"{_num(budget.limit)}{unit} (cut {_num(over)}{unit})"
    )


def pin_line(head_sha: str, linked_issues: tuple[int, ...] = ()) -> str:
    """The first line of every review: the exact commit judged.

    Empty when no head is known, so a caller without one never prints a
    made-up sha."""
    sha = (head_sha or "").strip()
    if not sha:
        return ""
    issues = ", ".join(f"#{n}" for n in linked_issues)
    suffix = f" for {issues}" if issues else ""
    return f"{PIN_PREFIX} `{sha}`{suffix}."


def _first_sentence(text: str) -> str:
    flat = " ".join((text or "").split())
    for stop in (". ", " -- "):
        head, sep, _ = flat.partition(stop)
        if sep:
            flat = head
    flat = flat.rstrip(".")
    if len(flat) > _TARGET_CHARS:
        flat = flat[: _TARGET_CHARS - 3].rstrip() + "..."
    return flat


def fix_target(finding: Finding) -> str:
    """The concrete change that clears this marking."""
    if finding.budgets:
        return "; ".join(budget_text(b) for b in finding.budgets)
    if finding.suggestion and "\n" not in finding.suggestion.strip():
        return _first_sentence(finding.suggestion)
    return _first_sentence(finding.message)


def finding_line(index: int, finding: Finding) -> str:
    """One numbered list entry; a check-sourced marking carries the check's
    own output line under it."""
    where = _safe_span(f"{finding.file}:{finding.line}")
    line = f"{index}. {where} {_safe_span(finding.rule_name)} - {fix_target(finding)}"
    if finding.check_output:
        line += f"\n   Check output: {_safe_span(finding.check_output)}"
    return line


def _split(evaluation: CodeReviewEvaluation) -> tuple[
    tuple[Finding, ...], tuple[Finding, ...], tuple[Finding, ...],
]:
    in_diff = tuple(f for f in evaluation.findings if f.outside_diff_reason is None)
    required = tuple(f for f in in_diff if f.severity in _BLOCKING_SEVERITIES)
    advisory = tuple(f for f in in_diff if f.severity not in _BLOCKING_SEVERITIES)
    follow_ups = tuple(
        f for f in evaluation.findings if f.outside_diff_reason is not None
    )
    return required, advisory, follow_ups


def _numbered(title: str, findings: tuple[Finding, ...], start: int) -> list[str]:
    if not findings:
        return []
    lines = [f"**{title}**", ""]
    for i, f in enumerate(findings[:_LIST_CAP], start=start):
        lines.append(finding_line(i, f))
    if len(findings) > _LIST_CAP:
        lines.append(f"(+{len(findings) - _LIST_CAP} more in the table above)")
    lines.append("")
    return lines


def closure_line(evaluation: CodeReviewEvaluation) -> str:
    """The explicit last line of the review."""
    if is_real_degradation(evaluation.degraded_reason):
        return CLOSURE_DEGRADED
    required, advisory, _ = _split(evaluation)
    if required:
        return CLOSURE_COMPLETE
    if advisory:
        return CLOSURE_ADVISORY_ONLY
    return CLOSURE_NOTHING


def pinned_tail(evaluation: CodeReviewEvaluation) -> str:
    """Required fixes, advisory markings, follow-ups to file, then closure."""
    required, advisory, follow_ups = _split(evaluation)
    lines: list[str] = []
    lines += _numbered("Required fixes", required, 1)
    lines += _numbered("Advisory (optional)", advisory, len(required[:_LIST_CAP]) + 1)
    if follow_ups:
        lines += ["**Follow-up issues to file (non-blocking, outside this diff)**", ""]
        for f in follow_ups[:_LIST_CAP]:
            where = _safe_span(f"{f.file}:{f.line}")
            lines.append(
                f"- {where} {_safe_span(f.rule_name)} - {_first_sentence(f.message)}"
            )
        if len(follow_ups) > _LIST_CAP:
            lines.append(f"- (+{len(follow_ups) - _LIST_CAP} more)")
        lines.append("")
    lines.append(closure_line(evaluation))
    return "\n".join(lines)


def pin_review(
    body: str, evaluation: CodeReviewEvaluation, *,
    head_sha: str, linked_issues: tuple[int, ...] = (),
) -> str:
    """`body` framed in the pinned style: pin line first, closure last."""
    parts = [pin_line(head_sha, linked_issues), body.strip(), pinned_tail(evaluation)]
    return "\n\n".join(p for p in parts if p)
