"""Deterministic post-filter for intent-mismatch findings (#1170).

The Elder prompt teaches that a negated acceptance criterion ("no X",
"never X", ...) is MET when the diff removes or prevents the forbidden
thing, and that every intent-mismatch finding must quote both the
criterion and the PR sentence it claims contradicts it. This module is
the deterministic backstop for when the model still raises: it runs on
the LLM findings right after ``evaluate_diff`` (see the inline
``filter_intent_mismatch_findings`` call in
``dispatch_code_review`` in ``dispatch.py``), before the judge gate.

Pure: findings in, findings out. No IO. Fail-open: an unexpected shape
keeps the finding rather than dropping it.

Rules, in order:
1. A finding that does not quote BOTH texts (criterion + PR sentence) is
   dropped - it carries no checkable evidence.
2. A finding whose two quotes share the SAME negation (both say "no" or
   "never" about the same thing) is lowered to medium: advisory, never
   merge-blocking. This is the recorded #1170 shape.
3. Everything else (including a PR that ADDS the forbidden thing) passes
   through untouched.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable

from personas.code_reviewer.persona import Finding

INTENT_MISMATCH = "intent-mismatch"

# A quote is a double-quoted span long enough to carry a real sentence,
# not a lone identifier the model quoted for emphasis. 12 is the tuned
# floor: shorter spans ("id", "row") are emphasis, not evidence.
_QUOTED_SPAN_RE = re.compile(r'"([^"\n]{12,})"')

# Negation vocabulary shared by criteria ("no X") and PR summaries
# ("stop writing X"). Includes removal verbs: a PR that deletes the
# forbidden code is describing the same prohibition, not defying it.
_NEGATION_RE = re.compile(
    r"\b(?:no|never|not|n't|cannot|can't|won't|without|"
    r"stops?|stopped|prevents?|prevented|forbids?|forbidden|"
    r"prohibits?|prohibited|removes?|removed|removal|"
    r"deletes?|deleted|deletion|drops?|dropped|eliminates?|eliminated|"
    r"ignores?|ignored)\b",
    re.IGNORECASE,
)

# Content words for the "about the same thing" check: alphabetic tokens
# of 5+ letters, minus the filler that would match on any two sentences.
_STOPWORDS = frozenset({
    "about", "after", "against", "being", "change", "criterion",
    "criteria", "could", "demand", "demands", "diff", "does", "doing",
    "having", "hunk", "issue", "message", "other", "quote", "quoted",
    "quotes", "say", "says", "sentence", "should", "speaks", "tablet",
    "these", "thing", "those", "under", "where", "which", "while",
    "would", "write", "writes",
})


def _quoted_spans(message: str) -> list[str]:
    """Double-quoted spans of sentence length, in order of appearance."""
    return [m.group(1).strip() for m in _QUOTED_SPAN_RE.finditer(message or "")]


def _content_words(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-zA-Z]{5,}", text.lower())
        if w not in _STOPWORDS
    }


def _action_negated(text: str) -> bool:
    """The quote's main action is negated, forbidden, or removed.

    Only the LEAD of the first clause counts: a "not" buried in a
    relative clause ("cohorts whose queue did not pop") describes the
    cohort, not the PR's action, and must not flip the polarity.
    """
    clause = re.split(r"[;,]", text, maxsplit=1)[0]
    lead = " ".join(re.findall(r"[a-zA-Z']+", clause.lower())[:4])
    return bool(_NEGATION_RE.search(lead))


def _shares_negation(first: str, second: str) -> bool:
    """Both quotes negate (or remove) the same thing.

    Guards the #1170 false positive: the criterion says "no leave rows"
    and the PR summary says "stop writing leave rows" - same negation,
    same topic, so the finding is a misread, not a real contradiction.
    A PR whose lead action is AFFIRMATIVE ("writes a leave row") while
    the criterion forbids it is the true positive and is never lowered.
    """
    if not (_action_negated(first) and _action_negated(second)):
        return False
    return bool(_content_words(first) & _content_words(second))


def filter_intent_mismatch_findings(
    findings: Iterable[Finding],
) -> tuple[tuple[Finding, ...], int, int]:
    """Apply the #1170 post-filter.

    Returns (kept, dropped, lowered). Lowered findings are re-emitted at
    medium severity; dropped findings carry no checkable evidence.
    Non-intent-mismatch findings pass through untouched.
    """
    kept: list[Finding] = []
    dropped = 0
    lowered = 0
    for finding in findings:
        if finding.rule_name != INTENT_MISMATCH:
            kept.append(finding)
            continue
        spans = _quoted_spans(finding.message)
        if len(spans) < 2:
            # No checkable evidence: the finding does not quote both the
            # criterion and the PR sentence the prompt requires.
            dropped += 1
            continue
        if _shares_negation(spans[0], spans[1]) and finding.severity == "high":
            kept.append(dataclasses.replace(finding, severity="medium"))
            lowered += 1
            continue
        kept.append(finding)
    return tuple(kept), dropped, lowered


# --- #1218: scope intent-mismatch to the boxes a Refs-only PR claims ------
#
# A PR that links an issue with `Refs #N` (not a closing keyword) is one
# slice of that issue. Elder graded such a PR against EVERY box on the
# issue, including boxes earlier PRs had already delivered and ticked, and
# blocked a correct change once per box, one re-run at a time. The rule:
# - a finding about a box already ticked on a Refs-only issue is dropped;
# - a finding about an open box the PR body does not claim is lowered to
#   medium (advisory, never merge-blocking);
# - a finding about an open box the PR body claims, or about any box of an
#   issue the PR closes, is untouched (the closing case is out of scope);
# - every finding the scope can attribute names the box and whether it is
#   ticked on the issue.
# Fail-open: a finding the scope cannot attribute to a box is kept as is,
# unless every linked issue is Refs-only and the body claims no box at all.

# (issue, criterion number, "open" | "ticked" | "delivered", cleaned text), the shape
# `linked_issue.rendered_criteria_with_state` returns.
LinkedBox = tuple[int, int, str, str]

_BOX_REF_RE = re.compile(
    r"#(\d+)[\s,:]*(?:acceptance\s+)?(?:criterion|criteria|box)\s*#?(\d+)\b"
    r"|(?:criterion|box)\s*#?(\d+)\s+(?:of|on|in|from)\s+(?:issue\s+)?#(\d+)\b",
    re.IGNORECASE,
)
_BARE_BOX_REF_RE = re.compile(r"\b(?:criterion|box)\s*#?(\d+)\b", re.IGNORECASE)
_MIN_TEXT_MATCH = 12
_CLAIM_PREFIX = 60


def _normalized(text: str) -> str:
    """Lowercase words joined by single spaces: punctuation and markup out."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _explicit_box_refs(text: str, single_issue: int | None) -> set[tuple[int, int]]:
    """(issue, criterion) pairs the text names by number."""
    refs: set[tuple[int, int]] = set()
    for m in _BOX_REF_RE.finditer(text or ""):
        if m.group(1):
            refs.add((int(m.group(1)), int(m.group(2))))
        else:
            refs.add((int(m.group(4)), int(m.group(3))))
    if not refs and single_issue is not None:
        refs = {(single_issue, int(m.group(1))) for m in _BARE_BOX_REF_RE.finditer(text or "")}
    return refs


def _boxes_for_finding(
    message: str, boxes: tuple[LinkedBox, ...], single_issue: int | None,
) -> list[LinkedBox]:
    """The linked boxes a finding is about: named by number, else quoted."""
    refs = _explicit_box_refs(message, single_issue)
    named = [b for b in boxes if (b[0], b[1]) in refs]
    if named:
        return named
    spans = [_normalized(s) for s in _quoted_spans(message)]
    spans = [s for s in spans if len(s) >= _MIN_TEXT_MATCH]
    quoted = []
    for box in boxes:
        text = _normalized(box[3])
        if len(text) >= _MIN_TEXT_MATCH and any(s in text or text in s for s in spans):
            quoted.append(box)
    return quoted


def _body_claims(box: LinkedBox, pr_body: str, single_issue: int | None) -> bool:
    """The PR body names this box by number or repeats its text."""
    if (box[0], box[1]) in _explicit_box_refs(pr_body, single_issue):
        return True
    text = _normalized(box[3])[:_CLAIM_PREFIX]
    return len(text) >= _MIN_TEXT_MATCH and text in _normalized(pr_body)


def body_claims_box(box: LinkedBox, pr_body: str, single_issue: int | None) -> bool:
    """Public form of the claim test, for the lane-work criteria scope
    (#1364): the PR body names this box by number or repeats its text."""
    return _body_claims(box, pr_body, single_issue)


def _box_label(box: LinkedBox) -> str:
    return f"#{box[0]} criterion {box[1]} ({box[2]} on the issue)"


def _annotated(finding: Finding, boxes: list[LinkedBox], note: str = "") -> Finding:
    noun = "Box" if len(boxes) == 1 else "Boxes"
    labels = ", ".join(_box_label(b) for b in boxes)
    tail = f" {noun}: {labels}." if boxes else ""
    return dataclasses.replace(finding, message=f"{finding.message}{tail}{note}")


_ADVISORY_NOTE = (
    " This PR links the issue with Refs, not a closing keyword, and its body "
    "does not claim this box, so the finding is advisory."
)


@dataclasses.dataclass(frozen=True, slots=True)
class _BoxScope:
    """What the scope knows about one PR's linked boxes."""

    boxes: tuple[LinkedBox, ...]
    closed: frozenset[int]
    single_issue: int | None
    pr_body: str
    any_claim: bool
    any_closed: bool

    def claims(self, box: LinkedBox) -> bool:
        return box[2] == "open" and _body_claims(box, self.pr_body, self.single_issue)


def _build_scope(
    boxes: tuple[LinkedBox, ...], closed_issues: Iterable[int], pr_body: str,
) -> _BoxScope:
    closed = frozenset(closed_issues)
    issues = {b[0] for b in boxes}
    single_issue = next(iter(issues)) if len(issues) == 1 else None
    body = pr_body or ""
    scope = _BoxScope(boxes, closed, single_issue, body, False, bool(issues & closed))
    any_claim = any(scope.claims(b) for b in boxes if b[0] not in closed)
    return dataclasses.replace(scope, any_claim=any_claim)


def _advisory(finding: Finding, boxes: list[LinkedBox]) -> Finding:
    return dataclasses.replace(
        _annotated(finding, boxes, _ADVISORY_NOTE), severity="medium",
    )


def _scoped(finding: Finding, scope: _BoxScope) -> tuple[Finding | None, str]:
    """One intent-mismatch under the scope: (finding or None, outcome), where
    outcome is "kept", "dropped" or "lowered"."""
    matched = _boxes_for_finding(finding.message, scope.boxes, scope.single_issue)
    blocking = finding.severity in ("high", "critical")
    if not matched:
        if blocking and not scope.any_closed and not scope.any_claim:
            return _advisory(finding, []), "lowered"
        return finding, "kept"
    # A merged PR on the ticket delivered these, by a heuristic: it stops the
    # block, even on the PR that closes the ticket, but stays visible.
    if all(b[2] == "delivered" for b in matched):
        return _advisory(finding, matched), "lowered"
    if any(b[0] in scope.closed for b in matched):
        return _annotated(finding, matched), "kept"
    if all(b[2] == "ticked" for b in matched):
        return None, "dropped"
    if not blocking or any(scope.claims(b) for b in matched):
        return _annotated(finding, matched), "kept"
    return _advisory(finding, matched), "lowered"


def scope_intent_mismatch_to_claimed_boxes(
    findings: Iterable[Finding],
    *,
    boxes: Iterable[LinkedBox],
    closed_issues: Iterable[int],
    pr_body: str,
) -> tuple[tuple[Finding, ...], int, int]:
    """Apply the #1218 scope. Returns (kept, dropped, lowered)."""
    scope = _build_scope(tuple(boxes), closed_issues, pr_body)
    kept: list[Finding] = []
    counts = {"kept": 0, "dropped": 0, "lowered": 0}
    for finding in findings:
        if finding.rule_name != INTENT_MISMATCH or not scope.boxes:
            kept.append(finding)
            continue
        result, outcome = _scoped(finding, scope)
        counts[outcome] += 1
        if result is not None:
            kept.append(result)
    return tuple(kept), counts["dropped"], counts["lowered"]
