# MIRRORED — sibling at services/webhook/activity_log.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Best-effort Activity-feed writer (PRD #301, Slice S1).

The ONE place both persona dispatchers record a Check verdict: map the legacy
persona code key to its caveman name (ADR-0002), derive the badge from raw
facts via the single `review_types.verdict` mapper (ADR-0003), and upsert it
through `install_store`.

**Best-effort / never-raise.** Recording activity must NEVER break a
check-run — same discipline as the #272 async offload and the capture-on-publish
comment writer. Any failure (DDB blip, an unexpected value) is logged and
swallowed so the caller's published verdict is unaffected.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from adapters.install_store import put_check_verdict
from review_types import persona_for_key, verdict

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.activity_log")


def record_check_verdict(
    *,
    install_id: int,
    persona_key: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    conclusion: str,
    summary: str,
    findings_count: int,
    blocking: bool,
    degraded_reason: Optional[str] = None,
) -> None:
    """Derive + persist one persona check-run's Check verdict.

    `persona_key` is the legacy code key (`tpm`/`code_reviewer`); it is mapped
    to the caveman name stored on the row. `findings_count` is whatever the
    caller weighs as actionable (failed blocking checks for Chief, findings for
    Elder) — see `review_types.verdict`. Idempotent per `(persona, head_sha)`
    at the store layer (re-review of the same commit heals the row).

    Best-effort: never raises. A failure is logged as `check_verdict_record_failed`
    and swallowed so the check-run the caller just published is never affected.
    """
    try:
        persona = persona_for_key(persona_key)
        v = verdict(
            conclusion=conclusion,
            findings_count=findings_count,
            degraded_reason=degraded_reason,
        )
        put_check_verdict(
            install_id=install_id,
            persona=persona,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            conclusion=conclusion,
            summary=summary,
            findings_count=findings_count,
            blocking=blocking,
            verdict=v,
            created_at=datetime.now(timezone.utc).isoformat(),
            degraded_reason=degraded_reason,
        )
    except Exception as e:  # noqa: BLE001
        # Intentional broad catch: this is a best-effort telemetry write on the
        # tail of a successful check-run. Recording must never raise back into
        # the dispatcher (mirrors put_comment_record's capture-best-effort and
        # the #272 never-raise offload). The row simply won't appear in the feed.
        log.warning(
            "check_verdict_record_failed",
            extra={
                "persona_key": persona_key,
                "repo": repo,
                "pr_number": pr_number,
                "error": f"{type(e).__name__}: {e}",
            },
        )
