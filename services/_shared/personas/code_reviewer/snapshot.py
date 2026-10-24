"""Backward-compat shim: re-exports the Elder snapshot identity from
`elder_review`, which now owns the full durable-attempt lifecycle.

Keep this module for existing import paths; new code should import
directly from `personas.code_reviewer.elder_review`.
"""

from __future__ import annotations

from personas.code_reviewer.elder_review import (  # noqa: F401
    adaptive_elder_settle_seconds,
    normalize_intent_text,
    review_freshness_id,
    review_freshness_id_from_pr,
    review_snapshot_id,
    review_snapshot_id_from_pr,
)

__all__ = [
    "adaptive_elder_settle_seconds",
    "normalize_intent_text",
    "review_freshness_id",
    "review_freshness_id_from_pr",
    "review_snapshot_id",
    "review_snapshot_id_from_pr",
]
