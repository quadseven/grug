"""Tests for github_oauth route handlers — login + me + logout.

Callback tests (token exchange + user upsert) deferred to v1.5
(needs httpx.MockTransport per #105). These cover the simpler routes
that can be tested by direct call + mocked deps.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def _oauth_mod(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET_SSM", "/grug/test-webhook-secret")
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID_SSM", "/grug/test-client-id")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET_SSM", "/grug/test-client-secret")
    monkeypatch.setenv("GRUG_DOMAIN", "grug.lol")
    import auth.github_oauth as mod
    monkeypatch.setattr(mod, "_state_secret", lambda: "test-secret-v1")
    monkeypatch.setattr(mod, "_session_secret", lambda: "test-session-v1")
    monkeypatch.setattr(mod, "_client_id", lambda: "Iv1.testclientid")
    monkeypatch.setattr(mod, "_client_secret", lambda: "test-client-secret")
    return mod


def test_login_redirects_to_github_authorize_url(_oauth_mod):
    resp = _oauth_mod.login()
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=Iv1.testclientid" in location
    assert "state=" in location
    assert "redirect_uri=https" in location


def test_login_sets_oauth_state_cookie(_oauth_mod):
    resp = _oauth_mod.login()
    set_cookie = resp.headers.get("set-cookie", "")
    assert "grug_oauth_state=" in set_cookie
    # Security cookie attrs
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie.lower() or "samesite=lax" in set_cookie.lower()


def test_login_state_cookie_value_round_trips_via_verify_state(_oauth_mod):
    """The state in the URL = the state in the cookie. _verify_state passes."""
    resp = _oauth_mod.login()
    location = resp.headers["location"]
    set_cookie = resp.headers["set-cookie"]
    # Extract state from URL query
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(location).query)
    state_in_url = qs["state"][0]
    # Extract state from Set-Cookie
    state_in_cookie = set_cookie.split("grug_oauth_state=", 1)[1].split(";", 1)[0]
    assert state_in_url == state_in_cookie
    assert _oauth_mod._verify_state(state_in_url)


def test_me_anonymous_returns_authenticated_false(_oauth_mod):
    with patch.object(_oauth_mod, "_verify_session", return_value=None):
        out = _oauth_mod.me("")
    assert out == {"authenticated": False}


def test_me_invalid_session_returns_authenticated_false(_oauth_mod):
    with patch.object(_oauth_mod, "_verify_session", return_value=None):
        out = _oauth_mod.me("garbage.cookie")
    assert out == {"authenticated": False}


def test_me_session_valid_but_user_missing_returns_authenticated_false(_oauth_mod):
    """User row deleted from DDB AFTER the session was minted — return
    not-authenticated rather than crash on None.user."""
    with patch.object(_oauth_mod, "_verify_session", return_value="100"):
        with patch.object(_oauth_mod, "get_user", return_value=None):
            out = _oauth_mod.me("valid-cookie")
    assert out == {"authenticated": False}


def test_me_returns_user_fields(_oauth_mod):
    from adapters.user_store import User
    user = User(
        github_user_id="100", login="evan", role="admin", tier="lifetime",
        allowlisted=True, oauth_access_token="x", oauth_refresh_token=None,
        created_at="",
    )
    with patch.object(_oauth_mod, "_verify_session", return_value="100"):
        with patch.object(_oauth_mod, "get_user", return_value=user):
            out = _oauth_mod.me("valid-cookie")
    assert out == {
        "authenticated": True,
        "github_user_id": "100",
        "login": "evan",
        "role": "admin",
        "tier": "lifetime",
        "allowlisted": True,
    }
    # Critical: never returns oauth tokens to /me
    assert "oauth_access_token" not in out
    assert "oauth_refresh_token" not in out


def test_logout_returns_204_and_clears_cookie(_oauth_mod):
    resp = _oauth_mod.logout()
    assert resp.status_code == 204
    set_cookie = resp.headers.get("set-cookie", "")
    assert "grug_session=" in set_cookie
    # delete_cookie sets max-age=0 OR expires in the past
    assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie or "expires=" in set_cookie.lower()
