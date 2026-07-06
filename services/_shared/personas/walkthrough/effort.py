"""Review-effort estimate for the PR walkthrough (#554).

Distinct vocabulary from `review_types.Effort` (a per-FINDING fix-effort
hint) - this is a whole-PR REVIEW-time estimate, LORE's "40+ Minutes" /
CodeRabbit's "~60 minutes" chip. A deterministic file/line-count
heuristic is the reliable baseline; the model's own judgment (returned
alongside its summary) overrides it ONLY when the value is itself in the
closed set - the same coercion-degrades-to-fallback discipline as #553,
so an off-vocabulary or hallucinated label can never reach the chip.
"""

from __future__ import annotations

from typing import Literal, get_args

ReviewEffort = Literal["quick", "moderate", "involved", "extensive"]
REVIEW_EFFORTS: frozenset[str] = frozenset(get_args(ReviewEffort))

_LABELS: dict[str, str] = {
    "quick": "quick (~5 min)",
    "moderate": "moderate (~15 min)",
    "involved": "involved (~30 min)",
    "extensive": "extensive (45+ min)",
}


def estimate_effort(
    *, file_count: int, lines_changed: int, model_effort: str | None = None,
) -> ReviewEffort:
    """The heuristic estimate, overridden by `model_effort` only when it is
    itself a member of the closed set (never an off-vocabulary passthrough)."""
    if model_effort in REVIEW_EFFORTS:
        return model_effort  # type: ignore[return-value]
    if file_count > 15 or lines_changed > 500:
        return "extensive"
    if file_count > 6 or lines_changed > 150:
        return "involved"
    if file_count > 2 or lines_changed > 30:
        return "moderate"
    return "quick"


def effort_label(effort: ReviewEffort) -> str:
    """Human-facing chip text for a closed-set effort value."""
    return _LABELS[effort]
