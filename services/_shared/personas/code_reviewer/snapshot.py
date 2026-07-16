"""Canonical identity for one Elder review input snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

# v2: intent text is normalized (HTML comments + whitespace noise stripped)
# so bot footers and auto-generated body rewrites no longer thrash mid-flight
# Elder reviews while human intent is unchanged.
_SNAPSHOT_VERSION = "elder-review-v2"

# HTML comments are almost always bot/tool footers (release notes blocks,
# metadata markers). They rewrite without changing the author's intent and
# used to force a brand-new snapshot_id -> mid-flight cancel storm.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MULTI_BLANK = re.compile(r"\n{3,}")


def normalize_intent_text(text: str) -> str:
    """Return intent text suitable for snapshot identity.

    Strips HTML comments and collapses blank-line noise. Real human prose
    (title and body) still participates in the hash after cleanup so an
    author rewriting the Why still re-triggers review.
    """
    cleaned = _HTML_COMMENT.sub("", text or "")
    cleaned = _MULTI_BLANK.sub("\n\n", cleaned)
    return cleaned.strip()


def review_snapshot_id(
    *,
    base_sha: str,
    head_sha: str,
    title: str,
    body: str,
) -> str:
    """Return an unambiguous, bounded identity for every reviewed input.

    Head SHA alone is insufficient: changing the base changes the diff, while
    changing title/body changes the intent supplied to the reviewer. JSON
    array encoding preserves field boundaries and exact text without relying
    on delimiter escaping. Intent fields are normalized first so ephemeral
    bot footers do not thrash the durable review lane.
    """
    material = json.dumps(
        [
            _SNAPSHOT_VERSION,
            base_sha,
            head_sha,
            normalize_intent_text(title),
            normalize_intent_text(body),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"v1:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def review_snapshot_id_from_pr(pr: Mapping[str, Any]) -> str:
    """Build the canonical identity from GitHub pull-request JSON."""
    return review_snapshot_id(
        base_sha=str((pr.get("base") or {}).get("sha") or ""),
        head_sha=str((pr.get("head") or {}).get("sha") or ""),
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
    )


def adaptive_elder_settle_seconds(
    pr: Mapping[str, Any],
    *,
    base_seconds: int,
) -> int:
    """Scale the quiet window to the size of the hunt (Swift Elder).

    Tiny PRs almost never get force-pushed mid-settle; waiting the full
    quiet window only adds empty latency. Large multi-file PRs keep the
    full base window so rapid push storms do not burn dual Cave arms on
    every intermediate head.

    When GitHub omits size stats (additions/deletions/changed_files all
    zero/absent), keep ``base_seconds`` — never invent a "tiny" path from
    missing data.
    """
    base = max(0, int(base_seconds))
    try:
        additions = int(pr.get("additions") or 0)
        deletions = int(pr.get("deletions") or 0)
        changed = int(pr.get("changed_files") or 0)
    except (TypeError, ValueError):
        return base
    churn = max(0, additions) + max(0, deletions)
    changed = max(0, changed)
    if changed == 0 and churn == 0:
        return base
    # Swift Hunt: small, focused PR — start deep review immediately.
    if changed <= 3 and churn <= 80:
        return 0
    # Steady Hunt: medium PR — short settle only.
    if changed <= 8 and churn <= 300:
        return min(base, 5)
    # Full Hunt: large / noisy PR — full quiet window.
    return base
