"""Tests for github_oauth route handlers — login + me + logout.

The callback tests at the end mock httpx.post / httpx.get at the module
boundary and cover every exit: state mismatch, upstream failure on the
token and user endpoints, a missing token, a malformed user payload and
the success path (#1636).
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
    # CfAuthMiddleware now fail-CLOSES by default (audit #4). These tests
    # exercise the OAuth router, not the CF boundary, so run them in
    # bring-up mode (boundary effectively disabled) — the CF boundary has
    # its own suite in test_cf_auth.py.
    monkeypatch.setenv("GRUG_CF_AUTH_FAIL_OPEN", "1")
    import auth.github_oauth as mod
    # State CSRF + session cookie signing both use `_state_secret` —
    # no separate `_session_secret` function exists. The session HMAC
    # binds gh_id (Codex P1 fix in Slice 7) but uses the same key.
    monkeypatch.setattr(mod, "_state_secret", lambda: "test-secret-v1")
    monkeypatch.setattr(mod, "_client_id", lambda: "Iv1.testclientid")
    monkeypatch.setattr(mod, "_client_secret", lambda: "test-client-secret")
    # The per-IP limit has its own suite (test_oauth_rate_limit.py); admit
    # every request here so the router tests need no Postgres.
    from adapters.pg_rate_limit_store import ReservationResult
    monkeypatch.setattr(
        mod, "try_reserve_slot",
        lambda **_kw: ReservationResult(admitted=True, minute_count=1, day_count=1),
    )
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
    from adapters.user_store import UserIdentity
    user = UserIdentity(
        github_user_id="100", login="alice", role="admin", tier="lifetime",
        allowlisted=True, created_at="",
        allowlisted_at=None, allowlisted_by=None,
    )
    with patch.object(_oauth_mod, "_verify_session", return_value="100"):
        with patch.object(_oauth_mod, "get_user", return_value=user):
            out = _oauth_mod.me("valid-cookie")
    assert out == {
        "authenticated": True,
        "github_user_id": "100",
        "login": "alice",
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


# ── TestClient end-to-end tests cover FastAPI router wiring ──
# code-reviewer flagged that direct-handler-call tests bypass the
# router (a typo'd @router.get path would still pass). At least one
# happy-path TestClient call per route catches that drift.

def test_logout_via_test_client(_oauth_mod):
    """Real router round-trip — catches @router.post path typos +
    middleware regressions."""
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    r = client.post("/api/v1/auth/logout")
    assert r.status_code == 204
    # cookie cleared via Set-Cookie header (TestClient strips on ack)
    assert any(
        c.lower().startswith("grug_session=")
        for c in r.headers.get_list("set-cookie")
    )


def test_me_anonymous_via_test_client(_oauth_mod):
    """Real router round-trip — anonymous /me returns the documented shape."""
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    r = client.get("/api/v1/me")
    assert r.status_code == 200
    assert r.json() == {"authenticated": False}


def test_login_via_test_client(_oauth_mod):
    """Real router round-trip — login redirects + sets cookie."""
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    r = client.get("/api/v1/auth/github/login", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://github.com/login/oauth/authorize?")
    assert any(
        c.startswith("grug_oauth_state=")
        for c in r.headers.get_list("set-cookie")
    )


def test_callback_non_json_token_response_is_502_not_500(_oauth_mod, monkeypatch):
    """A 200 from the token endpoint that is not JSON (an interstitial or
    gateway page) is an upstream failure; `.json()` used to raise out of
    the handler as a 500 with no log line."""
    from unittest.mock import MagicMock
    from fastapi import HTTPException

    monkeypatch.setattr(_oauth_mod, "_verify_state", lambda s: True)
    fake = MagicMock()
    fake.raise_for_status = MagicMock()
    fake.json = MagicMock(side_effect=ValueError("not json"))
    with patch("httpx.post", return_value=fake), pytest.raises(HTTPException) as e:
        _oauth_mod.callback(code="c", state="s", grug_oauth_state="s")
    assert e.value.status_code == 502


# --- callback branches (#1636) ----------------------------------------------

def _resp(json_body=None, status_error=None):
    from unittest.mock import MagicMock

    r = MagicMock()
    r.raise_for_status = MagicMock(side_effect=status_error)
    r.json = MagicMock(return_value=json_body)
    return r


def _status_error(code):
    import httpx

    req = httpx.Request("POST", "https://github.com/login/oauth/access_token")
    return httpx.HTTPStatusError(
        "upstream", request=req, response=httpx.Response(code, request=req),
    )


@pytest.mark.parametrize(
    "state,cookie",
    [("s", "other"), ("s", ""), ("", "")],
    ids=["mismatch", "no-cookie", "empty"],
)
def test_callback_state_mismatch_is_400_before_any_upstream_call(_oauth_mod, state, cookie):
    from fastapi import HTTPException

    with patch("httpx.post") as post, pytest.raises(HTTPException) as e:
        _oauth_mod.callback(code="c", state=state, grug_oauth_state=cookie)
    assert e.value.status_code == 400
    assert e.value.detail == "invalid_state"
    post.assert_not_called()


def test_callback_unsigned_state_is_400_even_when_cookie_matches(_oauth_mod):
    from fastapi import HTTPException

    with patch("httpx.post") as post, pytest.raises(HTTPException) as e:
        _oauth_mod.callback(code="c", state="forged", grug_oauth_state="forged")
    assert e.value.status_code == 400
    post.assert_not_called()


@pytest.mark.parametrize("failure", ["status", "transport"])
def test_callback_token_endpoint_failure_is_502(_oauth_mod, monkeypatch, failure):
    import httpx
    from fastapi import HTTPException

    monkeypatch.setattr(_oauth_mod, "_verify_state", lambda s: True)
    if failure == "status":
        kw = {"return_value": _resp(status_error=_status_error(503))}
    else:
        kw = {"side_effect": httpx.ConnectError("dns")}
    with patch("httpx.post", **kw), pytest.raises(HTTPException) as e:
        _oauth_mod.callback(code="c", state="s", grug_oauth_state="s")
    assert e.value.status_code == 502
    assert e.value.detail == "oauth_upstream_failed"


def test_callback_missing_access_token_is_400(_oauth_mod, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(_oauth_mod, "_verify_state", lambda s: True)
    body = {"error": "bad_verification_code"}
    with patch("httpx.post", return_value=_resp(body)), \
            patch("httpx.get") as get, \
            patch.object(_oauth_mod, "upsert_oauth_user") as upsert, \
            pytest.raises(HTTPException) as e:
        _oauth_mod.callback(code="c", state="s", grug_oauth_state="s")
    assert e.value.status_code == 400
    assert e.value.detail == "oauth_token_exchange_failed"
    get.assert_not_called()
    upsert.assert_not_called()


def test_callback_user_endpoint_failure_is_502(_oauth_mod, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(_oauth_mod, "_verify_state", lambda s: True)
    with patch("httpx.post", return_value=_resp({"access_token": "gho_x"})), \
            patch("httpx.get", return_value=_resp(status_error=_status_error(401))), \
            patch.object(_oauth_mod, "upsert_oauth_user") as upsert, \
            pytest.raises(HTTPException) as e:
        _oauth_mod.callback(code="c", state="s", grug_oauth_state="s")
    assert e.value.status_code == 502
    assert e.value.detail == "oauth_upstream_failed"
    upsert.assert_not_called()


def test_callback_user_payload_without_id_is_502(_oauth_mod, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(_oauth_mod, "_verify_state", lambda s: True)
    with patch("httpx.post", return_value=_resp({"access_token": "gho_x"})), \
            patch("httpx.get", return_value=_resp({"login": "alice"})), \
            patch.object(_oauth_mod, "upsert_oauth_user") as upsert, \
            pytest.raises(HTTPException) as e:
        _oauth_mod.callback(code="c", state="s", grug_oauth_state="s")
    assert e.value.status_code == 502
    assert e.value.detail == "oauth_user_malformed"
    upsert.assert_not_called()


def test_callback_success_upserts_and_sets_a_session_for_that_user(_oauth_mod):
    from adapters.user_store import UserIdentity

    login = _oauth_mod.login()
    state = login.headers["set-cookie"].split("grug_oauth_state=", 1)[1].split(";", 1)[0]
    user = UserIdentity(
        github_user_id="4242", login="alice", role="user", tier="free",
        allowlisted=False, created_at="", allowlisted_at=None, allowlisted_by=None,
    )
    token_body = {"access_token": "gho_access", "refresh_token": "ghr_refresh"}
    with patch("httpx.post", return_value=_resp(token_body)) as post, \
            patch("httpx.get", return_value=_resp({"id": 4242, "login": "alice"})) as get, \
            patch.object(_oauth_mod, "upsert_oauth_user", return_value=user) as upsert:
        resp = _oauth_mod.callback(code="the-code", state=state, grug_oauth_state=state)

    assert post.call_args.kwargs["data"]["code"] == "the-code"
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer gho_access"
    upsert.assert_called_once_with(
        github_user_id="4242", login="alice",
        oauth_access_token="gho_access", oauth_refresh_token="ghr_refresh",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == _oauth_mod._DASHBOARD_URL
    cookies = resp.headers.getlist("set-cookie")
    session = next(c for c in cookies if c.startswith("grug_session="))
    assert "HttpOnly" in session and "Secure" in session
    value = session.split("grug_session=", 1)[1].split(";", 1)[0]
    assert _oauth_mod._verify_session(value) == "4242"
    assert any(c.startswith("grug_oauth_state=") for c in cookies)
