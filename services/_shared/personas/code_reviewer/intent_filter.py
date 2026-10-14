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

_INTENT_MISMATCH = "intent-mismatch"

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
        if finding.rule_name != _INTENT_MISMATCH:
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
