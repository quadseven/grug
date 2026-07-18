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
import pytest

from personas.publish_check import publish_persona_check


@pytest.fixture(autouse=True)
def _zero_retry_backoff(monkeypatch):
    """#697's transient-retry sleeps are real time.sleep calls; zero the
    backoff so the retry-path tests (and the pre-existing ConnectError
    tests, which now retry before failing) stay instant."""
    from personas import publish_check
    monkeypatch.setattr(publish_check, "_TRANSIENT_BACKOFF_BASE_S", 0.0)


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
        check_name="Grug - Guard",
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
    assert posted["result"].name == "Grug - Guard"
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
        check_name="Grug - Warder",
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
        check_name="Grug - Elder",
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
        check_name="Grug - Smasher",
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
            check_name="Grug - Guard",
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
            check_name="Grug - Guard",
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
            check_name="Grug - Guard",
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
            check_name="Grug - Guard",
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
            check_name="Grug - Guard",
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
        check_name="Grug - Chief",
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
            check_name="Grug - Guard",
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


# --- transient-network retry (#697) -----------------------------------

def _persona_kwargs(persona: str) -> dict:
    """Shared kwargs for the retry tests - persona-parameterized so the
    Chief and Guard cases (#697's two named victims) read identically."""
    return dict(
        persona_key=persona,
        persona_prefix=persona,
        check_name=f"Grug - {persona}",
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
        publish_failed_log_name=f"{persona}_publish_failed",
    )


@pytest.mark.parametrize("persona", ["tpm", "guard"])
def test_transient_connect_error_retries_then_succeeds(monkeypatch, persona):
    """#697: a one-shot DNS/connect blip (the digital-ledger#204 incident)
    must be absorbed by the bounded retry, not leave the check-run
    permanently un-posted."""
    from personas import publish_check

    calls = {"n": 0}

    def _flaky_retry(installation_id, fn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("[Errno -5] No address associated with hostname")
        return fn("tok")

    monkeypatch.setattr(publish_check, "with_install_token_retry", _flaky_retry)
    monkeypatch.setattr(
        publish_check, "post_check_run",
        lambda token, owner, repo, result, external_id=None: {},
    )
    monkeypatch.setattr(publish_check, "record_check_verdict", lambda **kw: None)

    out = publish_persona_check(**_persona_kwargs(persona))

    assert out == {"persona": persona, "result": "pass"}
    assert calls["n"] == 2


def test_transient_retries_exhausted_emits_gauge_and_fails(monkeypatch, caplog):
    """#697: an outage that outlasts the bounded budget must fail LOUDLY -
    the retry warnings, the exhaustion gauge, and the existing
    publish-failed error log all fire; the result is still the honest
    publish_failed sentinel."""
    from personas import publish_check

    calls = {"n": 0}

    def _always_down(installation_id, fn):
        calls["n"] += 1
        raise httpx.ConnectError("[Errno -5] No address associated with hostname")

    gauges = []
    monkeypatch.setattr(publish_check, "with_install_token_retry", _always_down)
    monkeypatch.setattr(publish_check, "record_check_verdict", lambda **kw: None)
    monkeypatch.setattr(
        "observability.emit_gauge",
        lambda metric, value, tags=None: gauges.append((metric, value, tags)),
    )

    with caplog.at_level(logging.WARNING):
        out = publish_persona_check(**_persona_kwargs("tpm"))

    assert out == {"persona": "tpm", "result": "publish_failed"}
    # 1 initial attempt + _TRANSIENT_RETRIES retries, all failed
    assert calls["n"] == 1 + publish_check._TRANSIENT_RETRIES
    retry_logs = [r for r in caplog.records
                  if r.getMessage() == "check_publish_transient_retry"]
    assert len(retry_logs) == publish_check._TRANSIENT_RETRIES
    assert gauges == [
        ("grug.check_publish.transient_retries_exhausted", 1, {"persona": "tpm"}),
    ]


def test_http_status_error_does_not_retry(monkeypatch):
    """A real 4xx/5xx RESPONSE from GitHub is not a transient transport
    failure - retrying it would burn the webhook ACK window for no win.
    Exactly one attempt, straight to the honest failure path."""
    from personas import publish_check

    calls = {"n": 0}

    def _hard_500(installation_id, fn):
        calls["n"] += 1
        raise httpx.HTTPStatusError(
            "500 Server Error",
            request=httpx.Request("POST", "https://api.github.com/x"),
            response=httpx.Response(500),
        )

    monkeypatch.setattr(publish_check, "with_install_token_retry", _hard_500)
    monkeypatch.setattr(publish_check, "record_check_verdict", lambda **kw: None)

    out = publish_persona_check(**_persona_kwargs("guard"))

    assert out == {"persona": "guard", "result": "publish_failed"}
    assert calls["n"] == 1


def test_env_number_malformed_value_falls_back_with_warning(monkeypatch, caplog):
    """Qodo review, PR #698: a malformed operator env value must degrade to
    the default with a warning, never crash the module import (this loads
    inside webhook startup)."""
    from personas import publish_check

    monkeypatch.setenv("GRUG_TEST_RETRY_KNOB", "two")
    with caplog.at_level(logging.WARNING):
        out = publish_check._env_number("GRUG_TEST_RETRY_KNOB", 2, cap=5)
    assert out == 2
    assert any(r.getMessage() == "publish_retry_env_invalid" for r in caplog.records)


def test_env_number_clamps_negative_and_oversized(monkeypatch):
    """Negative values would make time.sleep raise; oversized budgets would
    burn the 10s webhook ACK window - both clamp instead."""
    from personas import publish_check

    monkeypatch.setenv("GRUG_TEST_RETRY_KNOB", "-3")
    assert publish_check._env_number("GRUG_TEST_RETRY_KNOB", 2, cap=5) == 0.0
    monkeypatch.setenv("GRUG_TEST_RETRY_KNOB", "99")
    assert publish_check._env_number("GRUG_TEST_RETRY_KNOB", 2, cap=5) == 5.0


def test_env_number_rejects_non_finite_values(monkeypatch, caplog):
    """CodeRabbit, PR #698: NaN parses as a float and SURVIVES the clamp
    (every NaN comparison is False, so min/max pass it through) - int(nan)
    would then crash the import. Non-finite values must route through the
    same warning-and-fallback path as malformed strings."""
    from personas import publish_check

    for raw in ("NaN", "inf", "-inf"):
        monkeypatch.setenv("GRUG_TEST_RETRY_KNOB", raw)
        with caplog.at_level(logging.WARNING):
            out = publish_check._env_number("GRUG_TEST_RETRY_KNOB", 2, cap=5)
        assert out == 2, raw
    assert any(r.getMessage() == "publish_retry_env_invalid" for r in caplog.records)


def test_backoff_delay_sequence_is_exponential(monkeypatch):
    """CodeRabbit, PR #698: exercise the real backoff math (the autouse
    fixture zeroes the base for speed everywhere else). With base=0.5 and
    two failures before success, the recorded sleeps must be 0.5 then 1.0."""
    from personas import publish_check

    monkeypatch.setattr(publish_check, "_TRANSIENT_BACKOFF_BASE_S", 0.5)
    slept = []
    monkeypatch.setattr(publish_check.time, "sleep", slept.append)

    calls = {"n": 0}

    def _flaky_twice(installation_id, fn):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectError("blip")
        return fn("tok")

    monkeypatch.setattr(publish_check, "with_install_token_retry", _flaky_twice)
    monkeypatch.setattr(
        publish_check, "post_check_run",
        lambda token, owner, repo, result, external_id=None: {},
    )
    monkeypatch.setattr(publish_check, "record_check_verdict", lambda **kw: None)

    out = publish_persona_check(**_persona_kwargs("tpm"))

    assert out == {"persona": "tpm", "result": "pass"}
    assert slept == [0.5, 1.0]


def test_cumulative_backoff_budget_is_capped(monkeypatch):
    """CodeRabbit, PR #698: the per-knob caps alone still allowed
    retries=5 x base=2.0 -> 62s of cumulative sleep, blowing the 10s
    webhook ACK window. The total-sleep ceiling truncates the last delay
    and exhausts the retry once the budget is spent."""
    from personas import publish_check

    monkeypatch.setattr(publish_check, "_TRANSIENT_RETRIES", 5)
    monkeypatch.setattr(publish_check, "_TRANSIENT_BACKOFF_BASE_S", 2.0)
    slept = []
    monkeypatch.setattr(publish_check.time, "sleep", slept.append)
    monkeypatch.setattr(
        publish_check, "with_install_token_retry",
        _raising(httpx.ConnectError("down hard")),
    )
    monkeypatch.setattr(publish_check, "record_check_verdict", lambda **kw: None)
    monkeypatch.setattr("observability.emit_gauge", lambda *a, **kw: None)

    out = publish_persona_check(**_persona_kwargs("tpm"))

    assert out == {"persona": "tpm", "result": "publish_failed"}
    # 2.0 + 4.0 spends 6 of the 8s budget; the third delay truncates to 2.0
    # and the fourth attempt finds the budget exhausted - never 62s.
    assert slept == [2.0, 4.0, 2.0]
    assert sum(slept) <= publish_check._TRANSIENT_TOTAL_SLEEP_CAP_S
