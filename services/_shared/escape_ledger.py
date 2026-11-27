"""Trace and classify captured corrections into new ledger rows (#1568)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from escape_classifier import EscapeClass, ReviewRecord, classify_review_outcome
from escape_trace import EscapeTrace, TraceSources, trace_escape
from ledger import ESCAPE_REVIEWER

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
        if row.get("reviewer") != ESCAPE_REVIEWER or row.get("class") != ESCAPE_FINDING_CLASS:
            continue
        for part in str(row.get("evidence", "")).split():
            key, _, value = part.partition("=")
            if key == "correcting_pr" and value.isascii() and value.isdecimal():
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
    fresh = [
        p for p in pulls if cutoff <= p.merged_at <= now and p.number not in recorded
    ]
    return sorted(fresh, key=lambda p: (p.merged_at, p.number))


def sweep_escape_rows(
    repo: str,
    pulls: Sequence[CorrectingPull],
    sources: TraceSources,
    review_history: Mapping[int, Sequence[ReviewRecord]],
    existing_rows: Iterable[dict],
    *,
    now: datetime,
) -> tuple[list[dict], list[int]]:
    """Return new rows and correcting PR numbers awaiting review evidence."""
    done = recorded_correcting_prs(row for row in existing_rows if row.get("repo") == repo)
    merged_at = {pull.number: pull.merged_at for pull in pulls}
    planned, missing = [], []
    for correction in pending_correcting_prs(pulls, done, now=now):
        pull = sources.get_pull(correction.number)
        if pull is None:
            continue
        trace = trace_escape(pull, repo, sources)
        earlier = merged_at.get(trace.escaped_pr)
        if earlier is None or earlier >= correction.merged_at:
            continue
        if trace.escaped_pr not in review_history:
            missing.append(correction.number)
            continue
        outcome = classify_review_outcome(
            trace, findings=trace.touching_findings,
            review_history=review_history[trace.escaped_pr],
        )
        if outcome is not None:
            row = escape_row(repo, trace, outcome, ts=correction.merged_at.astimezone(UTC).isoformat())
            if row is not None:
                planned.append(row)
    return planned, missing
