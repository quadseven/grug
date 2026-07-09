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


def _raising(exc: BaseException):
    """Factory for a with_install_token_retry stand-in that raises `exc`
    instead of calling `fn` - shared by the non-httpx and RequestError
    exception-surface tests (CodeRabbit finding on PR #562)."""
    def _retry(installation_id, fn):
        raise exc
    return _retry


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
    assert posted["result"].title == "t"
    assert posted["result"].summary == "s"
    assert posted["result"].head_sha == "abc123"
    assert posted["result"].status == "completed"
    assert recorded["persona_key"] == "guard"
    assert recorded["repo"] == "o/r"
    assert recorded["pr_number"] == 7
    assert recorded["head_sha"] == "abc123"
    assert recorded["conclusion"] == "neutral"
    assert recorded["summary"] == "t"  # title, not summary - matches all 6 existing sites
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


def test_publish_failure_log_carries_status_code_and_error_detail(monkeypatch, caplog):
    """Qodo finding on PR #562: the publish-failure log must carry enough
    detail (status code + error string) to diagnose a real incident, not
    just the exception class name - while the event NAME stays byte-identical
    for the monitor contract."""
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

    record = next(
        r for r in caplog.records if r.getMessage() == "guard_check_run_publish_failed"
    )
    assert record.status_code == 500
    assert "500" in record.error or "Server Error" in record.error
    # #550 stage-2 contract: the seam's total boundary absorbs exceptions
    # that used to escape to per-persona final guards (which logged the
    # traceback) - the failure line itself must carry the stack frame for
    # DD error tracking, and head_sha so it stays self-sufficient even if
    # INFO lines are sampled away.
    assert record.exc_info is not None
    assert record.head_sha == "sha"


def test_record_check_verdict_raising_does_not_crash_publish(monkeypatch, caplog):
    """CodeRabbit finding on PR #562: the docstring promises record_check_verdict
    can't crash the tail even after a successful publish - defense-in-depth over
    activity_log's own never-raise contract."""
    from personas import publish_check

    monkeypatch.setattr(publish_check, "with_install_token_retry", _fake_retry_ok)
    monkeypatch.setattr(publish_check, "post_check_run", lambda *a, **kw: {})

    def _boom(**kw):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(publish_check, "record_check_verdict", _boom)

    with caplog.at_level(logging.ERROR):
        out = publish_persona_check(
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

    # The publish itself succeeded, so the result reflects that - it must
    # never crash or silently invert to publish_failed because recording
    # blew up.
    assert out == {"persona": "guard", "result": "pass"}
    assert any(
        r.getMessage() == "check_verdict_record_failed_unexpected"
        for r in caplog.records
    )


def test_success_result_cannot_collide_with_publish_failed_sentinel():
    """Type-design finding on PR #562: a future caller passing
    success_result="publish_failed" by mistake would make a CLEAN publish
    indistinguishable from a real one in the returned dict + Activity row."""
    import pytest

    from personas.publish_check import publish_persona_check as ppc

    with pytest.raises(ValueError, match="publish_failed"):
        ppc(
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
            success_result="publish_failed",
            publish_failed_log_name="guard_check_run_publish_failed",
        )


def test_inconsistent_conclusion_raises_before_any_network_call(monkeypatch):
    """CheckRunResult's own invariant (status='completed' requires a
    non-None conclusion) is a caller-contract violation - it must fail
    loud and uncaught, not get folded into a swallowed "publish_failed"."""
    import pytest

    from personas import publish_check

    called = []
    monkeypatch.setattr(
        publish_check, "with_install_token_retry",
        lambda *a, **kw: called.append(1),
    )

    with pytest.raises(ValueError):
        publish_check.publish_persona_check(
            persona_key="guard",
            persona_prefix="guard",
            check_name="Grug — Guard",
            installation_id=1,
            owner="o",
            repo="r",
            pr_number=2,
            head_sha="sha",
            conclusion=None,
            title="t",
            summary="s",
            findings_count=0,
            blocking=False,
            degraded_reason=None,
            success_result="pass",
            publish_failed_log_name="guard_check_run_publish_failed",
        )
    assert called == []  # never reached the network call


def test_non_httpx_exception_from_auth_chain_still_records_honest_verdict(monkeypatch):
    """Runtime-trace finding on PR #562: a token-exchange RuntimeError, an
    SSM botocore error, or a malformed-JSON response are all real, reachable
    failures from the publish boundary that are NOT httpx.HTTPStatusError/
    RequestError - the except must be total, not HTTP-shaped, or these
    silently skip record_check_verdict entirely."""
    from personas import publish_check

    recorded = {}
    monkeypatch.setattr(
        publish_check, "with_install_token_retry",
        _raising(RuntimeError("malformed token response")),
    )
    monkeypatch.setattr(
        publish_check, "record_check_verdict", lambda **kw: recorded.update(kw),
    )

    out = publish_check.publish_persona_check(
        persona_key="tpm",
        persona_prefix="tpm",
        check_name="Grug — Definition of Ready",
        installation_id=1,
        owner="o",
        repo="r",
        pr_number=2,
        head_sha="sha",
        conclusion="success",
        title="t",
        summary="s",
        findings_count=0,
        blocking=True,
        degraded_reason=None,
        success_result="pass",
        publish_failed_log_name="tpm_publish_failed",
    )

    assert out == {"persona": "tpm", "result": "publish_failed"}
    assert recorded["degraded_reason"] == "check_publish_failed"


def test_request_error_with_no_response_attribute_does_not_crash(monkeypatch, caplog):
    """httpx.RequestError subclasses (ConnectError, ConnectTimeout, ...) have
    NO `.response` attribute at all - a naive `e.response.status_code` would
    raise AttributeError from inside the except block itself."""
    from personas import publish_check

    monkeypatch.setattr(
        publish_check, "with_install_token_retry",
        _raising(httpx.ConnectError("connection refused")),
    )
    monkeypatch.setattr(publish_check, "record_check_verdict", lambda **kw: None)

    with caplog.at_level(logging.ERROR):
        out = publish_check.publish_persona_check(
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

    assert out == {"persona": "guard", "result": "publish_failed"}
    record = next(
        r for r in caplog.records if r.getMessage() == "guard_check_run_publish_failed"
    )
    assert record.status_code is None
