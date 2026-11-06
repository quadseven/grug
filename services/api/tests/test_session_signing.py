"""Regression tests for session HMAC binding (Codex P1, Slice 7 #28).

Earlier session format `rand.ts.sig.gh_id` left gh_id outside the HMAC,
so any holder of a valid session could swap the trailing component to
impersonate any user. New format binds gh_id into the signature.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _stub_secret(monkeypatch, request):
    """Avoid real SSM in unit tests by stubbing the state secret loader.

    Tests that exercise the real `_state_secret` resolution request the
    `real_state_secret` fixture instead and stub only the SSM read.
    """
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET_SSM", "/test/secret")
    if "real_state_secret" in request.fixturenames:
        yield
        return
    with patch(
        "auth.github_oauth._state_secret",
        return_value="unit-test-secret-do-not-deploy",
    ):
        yield


def test_make_then_verify_session_round_trip():
    from auth.github_oauth import _make_session, _verify_session
    tok = _make_session("12345")
    assert _verify_session(tok) == "12345"


def test_swap_gh_id_invalidates_signature():
    """The impersonation attack: take a valid session, swap gh_id."""
    from auth.github_oauth import _make_session, _verify_session
    tok = _make_session("12345")
    rand, ts, _gh_id, sig = tok.split(".")
    forged = f"{rand}.{ts}.99999.{sig}"
    assert _verify_session(forged) is None


def test_swap_signature_invalidates():
    from auth.github_oauth import _make_session, _verify_session
    tok = _make_session("12345")
    rand, ts, gh_id, _sig = tok.split(".")
    forged = f"{rand}.{ts}.{gh_id}.{'0' * 64}"
    assert _verify_session(forged) is None


def test_old_three_part_state_token_rejected():
    """An attacker might try to pass the CSRF state cookie as a session."""
    from auth.github_oauth import _make_state, _verify_session
    state = _make_state()  # rand.ts.sig (3 parts, no gh_id)
    assert _verify_session(state) is None


def test_empty_or_malformed_returns_none():
    from auth.github_oauth import _verify_session
    assert _verify_session("") is None
    assert _verify_session("a.b.c") is None
    assert _verify_session("a.b.c.d.e") is None
    assert _verify_session("a.b.c.d") is None  # bad sig + ts


def test_expired_session_rejected(monkeypatch):
    from auth.github_oauth import _make_session, _verify_session
    import auth.github_oauth as mod

    tok = _make_session("12345")
    # Fast-forward past 7-day TTL (+1s safety margin)
    real_time = __import__("time").time
    monkeypatch.setattr(mod.time, "time", lambda: real_time() + mod._SESSION_TTL_SECONDS + 1)
    assert _verify_session(tok) is None


def test_csrf_state_still_3_parts_unaffected():
    """Make sure the session refactor didn't break OAuth CSRF state."""
    from auth.github_oauth import _make_state, _verify_state
    state = _make_state()
    assert state.count(".") == 2
    assert _verify_state(state) is True


# --- Signing-key resolution (#1627) -----------------------------------

_DEDICATED = "/test/session-signing"
_WEBHOOK = "/test/secret"


@pytest.fixture
def real_state_secret(monkeypatch):
    """The real `_state_secret`, with a scripted SSM read and a clean cache."""
    import auth.github_oauth as mod
    import secrets_loader

    calls: list[str] = []
    script: dict[str, list[object]] = {}

    def fake_read(name: str) -> str:
        calls.append(name)
        outcome = script[name].pop(0) if len(script[name]) > 1 else script[name][0]
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)

    monkeypatch.setattr(secrets_loader, "_get_ssm_secure_string", fake_read)
    monkeypatch.setattr(mod, "_signing_secret_cache", None)
    script[_WEBHOOK] = ["webhook"]
    yield mod, calls, script
    mod._signing_secret_cache = None


def _not_found() -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": "missing"}},
        "GetParameter",
    )


def test_failed_dedicated_read_is_not_cached_and_retries(real_state_secret, monkeypatch):
    """A transient failure must not pin the pod to the webhook secret."""
    from fastapi import HTTPException

    mod, calls, script = real_state_secret
    monkeypatch.setenv("GRUG_SESSION_SIGNING_SECRET_SSM", _DEDICATED)
    script[_DEDICATED] = [RuntimeError("ThrottlingException"), "dedicated"]

    with pytest.raises(HTTPException) as exc:
        mod._state_secret()
    assert exc.value.status_code == 503
    assert mod._state_secret() == "dedicated"
    assert _WEBHOOK not in calls


def test_successful_dedicated_read_is_cached(real_state_secret, monkeypatch):
    mod, calls, script = real_state_secret
    monkeypatch.setenv("GRUG_SESSION_SIGNING_SECRET_SSM", _DEDICATED)
    script[_DEDICATED] = ["dedicated"]

    for _ in range(5):
        assert mod._state_secret() == "dedicated"
    assert calls == [_DEDICATED]


def test_unset_env_falls_back_to_webhook_secret(real_state_secret, monkeypatch, caplog):
    """Local dev: no dedicated param configured, so the webhook key signs."""
    mod, calls, _script = real_state_secret
    monkeypatch.delenv("GRUG_SESSION_SIGNING_SECRET_SSM", raising=False)

    with caplog.at_level("WARNING", logger="grug.api.auth"):
        assert mod._state_secret() == "webhook"
        assert mod._state_secret() == "webhook"
    assert calls == [_WEBHOOK]
    assert "session_signing_secret_unconfigured_using_fallback" in caplog.text


def test_unprovisioned_dedicated_param_falls_back(real_state_secret, monkeypatch):
    """ParameterNotFound is config, not a fault: every pod sees it the same
    way, so the documented migration fallback still applies."""
    mod, calls, script = real_state_secret
    monkeypatch.setenv("GRUG_SESSION_SIGNING_SECRET_SSM", _DEDICATED)
    script[_DEDICATED] = [_not_found()]

    assert mod._state_secret() == "webhook"
    assert mod._state_secret() == "webhook"
    assert calls == [_DEDICATED, _WEBHOOK]


def test_session_signing_outage_returns_503_from_me(real_state_secret, monkeypatch):
    """The route fails closed instead of signing with another key."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mod, calls, script = real_state_secret
    monkeypatch.setenv("GRUG_SESSION_SIGNING_SECRET_SSM", _DEDICATED)
    script[_DEDICATED] = [RuntimeError("AccessDenied")]
    app = FastAPI()
    app.include_router(mod.router)

    client = TestClient(app)
    client.cookies.set("grug_session", "a.1.2.sig")
    resp = client.get("/api/v1/me")
    assert resp.status_code == 503
    assert _WEBHOOK not in calls
