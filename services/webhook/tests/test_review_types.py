"""Tests for review_types — the single Severity source (#250) + the
Activity-feed verdict/persona mapping (PRD #301)."""
from __future__ import annotations

from typing import get_args

import pytest

import review_types


def test_severity_vocabulary_is_the_four_levels():
    """Lock the actual vocabulary — an accidental add/drop/rename of a
    level is a calibration change that should fail a test, not ship."""
    assert set(get_args(review_types.Severity)) == {
        "low", "medium", "high", "critical",
    }


def test_severities_frozenset_matches_the_literal():
    """SEVERITIES is derived from Severity (no hand-maintained copy)."""
    assert review_types.SEVERITIES == frozenset(get_args(review_types.Severity))
    assert isinstance(review_types.SEVERITIES, frozenset)


# ── Activity-feed verdict + persona mapping (PRD #301; ADR-0002/0003) ─────────


def test_verdict_vocabulary_is_the_four_badges():
    assert set(get_args(review_types.Verdict)) == {"block", "warn", "pass", "errored"}
    assert review_types.VERDICTS == frozenset(get_args(review_types.Verdict))


def test_verdict_degraded_is_errored_never_pass():
    """A degraded run (LLM outage) must NEVER read as pass ("no lies") —
    degraded_reason wins over every other input."""
    assert review_types.verdict(
        conclusion="neutral", findings_count=0, degraded_reason="all_failed"
    ) == "errored"
    assert review_types.verdict(
        conclusion="success", findings_count=0, degraded_reason="all_failed"
    ) == "errored"
    # degraded beats even a failure conclusion + findings
    assert review_types.verdict(
        conclusion="failure", findings_count=5, degraded_reason="parse_failed"
    ) == "errored"


def test_verdict_failure_is_block_and_wins_over_findings():
    assert review_types.verdict(
        conclusion="failure", findings_count=0, degraded_reason=None
    ) == "block"
    # a gated PR is block, not warn, even with findings
    assert review_types.verdict(
        conclusion="failure", findings_count=3, degraded_reason=None
    ) == "block"


def test_verdict_neutral_with_findings_is_warn():
    """Elder advisory mode posts neutral, but findings → warn (not pass)."""
    assert review_types.verdict(
        conclusion="neutral", findings_count=3, degraded_reason=None
    ) == "warn"


def test_verdict_clean_is_pass():
    """Neutral-clean and success-clean both read pass — the distinction the raw
    conclusion alone can't make (neutral is overloaded)."""
    assert review_types.verdict(
        conclusion="neutral", findings_count=0, degraded_reason=None
    ) == "pass"
    assert review_types.verdict(
        conclusion="success", findings_count=0, degraded_reason=None
    ) == "pass"


def test_verdict_non_success_conclusion_is_errored_never_pass():
    """A non-success, non-failure GitHub conclusion (cancelled/timed_out/
    action_required/skipped/stale) means Grug never concluded — it must read
    `errored`, never `pass` or `warn` ("no lies"). Only success+neutral are
    'clean' enough to be pass/warn."""
    for c in ("cancelled", "timed_out", "action_required", "skipped", "stale"):
        assert review_types.verdict(
            conclusion=c, findings_count=0, degraded_reason=None
        ) == "errored", c


def test_verdict_negative_findings_count_is_not_pass():
    """A defensive guard: a negative count (a caller subtraction bug) must
    never silently read as pass — any non-zero count is a finding signal."""
    assert review_types.verdict(
        conclusion="neutral", findings_count=-1, degraded_reason=None
    ) == "warn"


def test_persona_names_round_trip():
    assert review_types.persona_for_key("tpm") == "chief"
    assert review_types.persona_for_key("code_reviewer") == "elder"
    assert review_types.key_for_persona("chief") == "tpm"
    assert review_types.key_for_persona("elder") == "code_reviewer"
    for key in ("tpm", "code_reviewer"):
        assert review_types.key_for_persona(review_types.persona_for_key(key)) == key


def test_persona_mappers_reject_unknown():
    with pytest.raises(ValueError):
        review_types.persona_for_key("smasher")
    with pytest.raises(ValueError):
        review_types.key_for_persona("warder")
