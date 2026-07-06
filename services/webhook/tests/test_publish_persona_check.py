"""Shared publish-and-record seam tests (#549, epic #548).

`publish_persona_check` owns the construct-publish-classify-record-return
tail every check-run-posting persona used to hand-roll. These tests pin
the ADR-0003 "no lies" contract: a publish failure must still leave an
honest, recomputable Activity row (never a silent gap), and the caller's
own honest degraded_reason must survive untouched on a clean publish.
"""
from __future__ import annotations

import logging

import httpx

from personas.publish_check import publish_persona_check


def _fake_retry_ok(installation_id, fn):
    return fn("fake-token")


def _fake_retry_raises(installation_id, fn):
    raise httpx.HTTPStatusError(
        "500 Server Error",
        request=httpx.Request("POST", "https://api.github.com/x"),
        response=httpx.Response(500),
    )


def test_publish_success_records_honest_verdict_and_matching_external_id(monkeypatch):
    from personas import publish_check

    posted = {}
    recorded = {}
    monkeypatch.setattr(publish_check, "with_install_token_retry", _fake_retry_ok)
    monkeypatch.setattr(
        publish_check, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.update(
            {"token": token, "owner": owner, "repo": repo,
             "result": result, "external_id": external_id},
        ) or {},
    )
    monkeypatch.setattr(
        publish_check, "record_check_verdict", lambda **kw: recorded.update(kw),
    )

    out = publish_persona_check(
        persona_key="guard",
        persona_prefix="guard",
        check_name="Grug — Guard",
        installation_id=42,
        owner="o",
        repo="r",
        pr_number=7,
        head_sha="abc123",
        conclusion="neutral",
        title="t",
        summary="s",
        findings_count=2,
        blocking=False,
        degraded_reason=None,
        success_result="pass",
        publish_failed_log_name="guard_check_run_publish_failed",
    )

    assert out == {"persona": "guard", "result": "pass"}
    assert posted["external_id"] == "grug-guard:o/r#7:abc123"
    assert posted["result"].name == "Grug — Guard"
    assert posted["result"].conclusion == "neutral"
    assert recorded["persona_key"] == "guard"
    assert recorded["repo"] == "o/r"
    assert recorded["pr_number"] == 7
    assert recorded["head_sha"] == "abc123"
    assert recorded["findings_count"] == 2
    assert recorded["blocking"] is False
    assert recorded["degraded_reason"] is None


def test_success_preserves_callers_own_degraded_reason(monkeypatch):
    from personas import publish_check

    recorded = {}
    monkeypatch.setattr(publish_check, "with_install_token_retry", _fake_retry_ok)
    monkeypatch.setattr(
        publish_check, "post_check_run", lambda *a, **kw: {},
    )
    monkeypatch.setattr(
        publish_check, "record_check_verdict", lambda **kw: recorded.update(kw),
    )

    out = publish_persona_check(
        persona_key="warder",
        persona_prefix="warder",
        check_name="Grug — Warder",
        installation_id=1,
        owner="o",
        repo="r",
        pr_number=5,
        head_sha="sha",
        conclusion="neutral",
        title="t",
        summary="s",
        findings_count=0,
        blocking=False,
        degraded_reason="fetch_failed",
        success_result="skipped",
        publish_failed_log_name="warder_publish_failed",
    )

    # publish succeeded, so the caller's own honest reason is NOT
    # overwritten by the check_publish_failed override.
    assert recorded["degraded_reason"] == "fetch_failed"
    assert out == {"persona": "warder", "result": "skipped"}


def test_publish_failure_records_check_publish_failed_and_returns_publish_failed(
    monkeypatch,
):
    from personas import publish_check

    recorded = {}
    monkeypatch.setattr(publish_check, "with_install_token_retry", _fake_retry_raises)
    monkeypatch.setattr(
        publish_check, "record_check_verdict", lambda **kw: recorded.update(kw),
    )

    out = publish_persona_check(
        persona_key="code_reviewer",
        persona_prefix="cr",
        check_name="Grug — Code Review",
        installation_id=1,
        owner="o",
        repo="r",
        pr_number=9,
        head_sha="deadbeef",
        conclusion="neutral",
        title="t",
        summary="s",
        findings_count=3,
        blocking=False,
        degraded_reason=None,
        success_result="pass",
        publish_failed_log_name="code_review_check_run_publish_failed",
    )

    assert out == {"persona": "code_reviewer", "result": "publish_failed"}
    assert recorded["degraded_reason"] == "check_publish_failed"
    assert recorded["persona_key"] == "code_reviewer"


def test_publish_failure_preserves_evaluations_own_degraded_reason_via_merge(
    monkeypatch,
):
    """A caller with its OWN degraded_reason (e.g. an already-degraded
    evaluation) wins over check_publish_failed — the merge is `degraded_reason
    or check_publish_failed`, never the other way."""
    from personas import publish_check

    recorded = {}
    monkeypatch.setattr(publish_check, "with_install_token_retry", _fake_retry_raises)
    monkeypatch.setattr(
        publish_check, "record_check_verdict", lambda **kw: recorded.update(kw),
    )

    publish_persona_check(
        persona_key="smasher",
        persona_prefix="smasher",
        check_name="Grug — Smasher",
        installation_id=1,
        owner="o",
        repo="r",
        pr_number=2,
        head_sha="sha",
        conclusion="neutral",
        title="t",
        summary="s",
        findings_count=0,
        blocking=False,
        degraded_reason="sandbox_unavailable",
        success_result="skipped",
        publish_failed_log_name="smasher_check_run_publish_failed",
    )

    assert recorded["degraded_reason"] == "sandbox_unavailable"


def test_publish_failure_emits_persona_named_log_line_verbatim(monkeypatch, caplog):
    """Monitor-contract: DD monitors key on the exact log event name each
    persona used before migration — the seam must emit it byte-identical,
    passed in by the caller."""
    from personas import publish_check

    monkeypatch.setattr(publish_check, "with_install_token_retry", _fake_retry_raises)
    monkeypatch.setattr(publish_check, "record_check_verdict", lambda **kw: None)

    with caplog.at_level(logging.ERROR):
        publish_persona_check(
            persona_key="guard",
            persona_prefix="guard",
            check_name="Grug — Guard",
            installation_id=1,
            owner="o",
            repo="r",
            pr_number=2,
            head_sha="sha",
            conclusion="neutral",
            title="t",
            summary="s",
            findings_count=0,
            blocking=False,
            degraded_reason=None,
            success_result="pass",
            publish_failed_log_name="guard_check_run_publish_failed",
        )

    assert any(
        r.getMessage() == "guard_check_run_publish_failed" for r in caplog.records
    )
