"""Escape ledger rows are built purely and are idempotent per correcting PR."""

from datetime import UTC, datetime, timedelta

from escape_classifier import EscapeClass
from escape_ledger import (
    CorrectingPull,
    escape_row,
    pending_correcting_prs,
    recorded_correcting_prs,
)
from escape_trace import EscapeTrace
from ledger import parse_row

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _trace(escaped_pr=10):
    return EscapeTrace(correcting_pr=20, escaped_pr=escaped_pr, link="revert-body")


def test_row_parses_as_a_ledger_row_on_the_escaped_pr():
    row = escape_row("o/r", _trace(), EscapeClass.NOT_SEEN)
    parsed = parse_row(row)
    assert parsed is not None
    assert parsed.pr == 10
    assert parsed.finding == "not-seen"


def test_untraced_escape_yields_no_row():
    assert escape_row("o/r", _trace(None), EscapeClass.NOT_REVIEWED) is None


def test_recorded_prs_round_trip_and_ignore_other_reviewers():
    row = escape_row("o/r", _trace(), EscapeClass.SEEN_NOT_FLAGGED)
    other = {"reviewer": "elder", "evidence": "correcting_pr=99"}
    assert recorded_correcting_prs([row, other]) == frozenset({20})


def test_pending_skips_recorded_and_old_and_sorts():
    pulls = [
        CorrectingPull(3, NOW - timedelta(days=1)),
        CorrectingPull(2, NOW - timedelta(days=5)),
        CorrectingPull(1, NOW - timedelta(days=91)),
        CorrectingPull(4, NOW - timedelta(days=2)),
    ]
    out = pending_correcting_prs(pulls, frozenset({4}), now=NOW)
    assert [p.number for p in out] == [2, 3]
