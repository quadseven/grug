"""Turn a classified escape into one idempotent ledger row (#1568 slice 3).

Pure: no network, no store. The sweep that fetches merged fix and revert pull
requests and appends the rows is a separate step; this module decides what row
a trace yields and which correcting pull requests are still unrecorded.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from escape_classifier import EscapeClass
from escape_trace import EscapeTrace

ESCAPE_REVIEWER = "escape-loop"
ESCAPE_FINDING_CLASS = "escape"
SWEEP_WINDOW_DAYS = 90


@dataclass(frozen=True, slots=True)
class CorrectingPull:
    """A merged fix or revert pull request a sweep may trace."""

    number: int
    merged_at: datetime


def escape_row(
    repo: str,
    trace: EscapeTrace,
    outcome: EscapeClass,
    *,
    ts: str = "",
) -> dict | None:
    """The ledger dict for one classified escape, or None when untraced.

    The row's ``pr`` is the escaped (originating) pull request. The correcting
    pull request is named in ``evidence`` as ``correcting_pr=<n>``, which is
    the idempotency key read back by ``recorded_correcting_prs``.
    """
    if trace.escaped_pr is None:
        return None
    return {
        "repo": repo,
        "pr": trace.escaped_pr,
        "reviewer": ESCAPE_REVIEWER,
        "severity": "",
        "class": ESCAPE_FINDING_CLASS,
        "finding": outcome.value,
        "verdict": "accepted",
        "evidence": f"correcting_pr={trace.correcting_pr} link={trace.link or ''}",
        "ts": ts,
        "category": "general",
    }


def recorded_correcting_prs(rows: Iterable[dict]) -> frozenset[int]:
    """Correcting PR numbers already stored as escape rows."""
    done: set[int] = set()
    for row in rows:
        if row.get("reviewer") != ESCAPE_REVIEWER:
            continue
        for part in str(row.get("evidence", "")).split():
            key, _, value = part.partition("=")
            if key == "correcting_pr" and value.isdigit():
                done.add(int(value))
    return frozenset(done)


def pending_correcting_prs(
    pulls: Sequence[CorrectingPull],
    recorded: frozenset[int],
    *,
    now: datetime,
    window_days: int = SWEEP_WINDOW_DAYS,
) -> list[CorrectingPull]:
    """Pulls merged inside the window and not yet recorded, oldest first."""
    cutoff = now - timedelta(days=window_days)
    fresh = [p for p in pulls if p.merged_at >= cutoff and p.number not in recorded]
    return sorted(fresh, key=lambda p: (p.merged_at, p.number))
