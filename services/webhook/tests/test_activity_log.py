"""Tests for activity_log.record_check_verdict — the best-effort Activity-feed
writer (PRD #301, Slice S1)."""
from __future__ import annotations

import logging

from unittest.mock import patch

import activity_log


def _capture(**overrides):
    """Run record_check_verdict with put_check_verdict mocked; return the item
    kwargs it was called with (or None if not called)."""
    seen: dict = {}

    def fake_put(**kw):
        seen.update(kw)

    base = dict(
        install_id=1, persona_key="code_reviewer", repo="o/r", pr_number=7,
        head_sha="abc", conclusion="neutral", summary="t",
        findings_count=3, blocking=False, degraded_reason=None,
    )
    base.update(overrides)
    with patch.object(activity_log, "put_check_verdict", side_effect=fake_put):
        activity_log.record_check_verdict(**base)
    return seen


def test_record_maps_persona_and_forwards_facts():
    """activity_log maps the persona key and forwards the RAW facts; the store
    derives the verdict, so `verdict` is NOT passed by the writer."""
    seen = _capture(persona_key="code_reviewer", findings_count=3, conclusion="neutral")
    assert seen["persona"] == "elder"        # mapped from the legacy code key
    assert seen["conclusion"] == "neutral"
    assert seen["findings_count"] == 3
    assert "created_at" in seen              # stamped by the writer
    assert "verdict" not in seen            # derivation is the store's job


def test_record_forwards_degraded_reason_and_maps_chief():
    seen = _capture(persona_key="tpm", degraded_reason="all_failed", findings_count=0)
    assert seen["persona"] == "chief"
    assert seen["degraded_reason"] == "all_failed"


def test_record_best_effort_swallows_store_failure(caplog):
    """A store failure must NOT propagate — recording activity can never break
    the check-run the caller just published."""
    caplog.set_level(logging.WARNING)
    with patch.object(
        activity_log, "put_check_verdict", side_effect=RuntimeError("ddb down")
    ):
        # must NOT raise
        activity_log.record_check_verdict(
            install_id=1, persona_key="code_reviewer", repo="o/r", pr_number=7,
            head_sha="abc", conclusion="neutral", summary="t",
            findings_count=0, blocking=False,
        )
    assert "check_verdict_record_failed" in [r.getMessage() for r in caplog.records]


def test_record_best_effort_swallows_unknown_persona_key(caplog):
    """An unknown persona key (mapping ValueError) is also swallowed — the
    write is entirely best-effort."""
    caplog.set_level(logging.WARNING)
    # put should never be reached; if mapping raised outside the guard this
    # would propagate.
    with patch.object(activity_log, "put_check_verdict") as put:
        activity_log.record_check_verdict(
            install_id=1, persona_key="bogus", repo="o/r", pr_number=7,
            head_sha="abc", conclusion="neutral", summary="t",
            findings_count=0, blocking=False,
        )
        put.assert_not_called()
    assert "check_verdict_record_failed" in [r.getMessage() for r in caplog.records]
