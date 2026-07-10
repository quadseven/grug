"""Canonical identity for one Elder review input snapshot."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

_SNAPSHOT_VERSION = "elder-review-v1"


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
    on delimiter escaping.
    """
    material = json.dumps(
        [_SNAPSHOT_VERSION, base_sha, head_sha, title, body],
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
