"""Regression tests for session HMAC binding.

Earlier session format `rand.ts.sig.gh_id` left gh_id outside the HMAC,
so any holder of a valid session could swap the trailing component to
impersonate any user. New format binds gh_id into the signature.

Sentry CRITICAL + Codex P1 on PR #39 / Slice 3.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _stub_secret(monkeypatch):
    """Avoid real SSM in unit tests by stubbing the state secret loader."""
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET_SSM", "/test/secret")
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
