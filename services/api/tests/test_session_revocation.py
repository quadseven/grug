"""Tests for per-user session generation revocation (#1681)."""

from __future__ import annotations

from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from adapters.user_store import UserIdentity
from main import app


def _user(*, role: str = "user", gen: int = 1) -> UserIdentity:
    return UserIdentity(
        github_user_id="100",
        login="alice",
        role=role,
        tier="free",
        allowlisted=True,
        created_at="2026-01-01T00:00:00Z",
        allowlisted_at=None,
        allowlisted_by=None,
        session_generation=gen,
    )


def test_session_token_carries_generation_and_rejects_older_generation(monkeypatch):
    import auth.github_oauth as oauth

    monkeypatch.setattr(oauth, "_state_secret", lambda: "unit-test-secret-do-not-deploy")
    tok_v1 = oauth._make_session("100", generation=1)
    assert oauth._verify_session(tok_v1) == "100"
    assert oauth._token_generation(tok_v1) == 1

    store_gen = {"100": 1}
    monkeypatch.setattr(oauth, "get_session_generation", lambda uid: store_gen.get(uid))
    oauth.invalidate_session_generation_cache("100")

    assert oauth._verify_active_session(tok_v1) == "100"

    # Bump stored generation and clear cache -> old token is refused.
    store_gen["100"] = 2
    oauth.invalidate_session_generation_cache("100")
    assert oauth._verify_active_session(tok_v1) is None

    # New token issued at generation 2 succeeds.
    tok_v2 = oauth._make_session("100", generation=2)
    assert oauth._verify_active_session(tok_v2) == "100"


def test_generation_cache_expires_within_60_seconds(monkeypatch):
    import auth.github_oauth as oauth

    monkeypatch.setattr(oauth, "_state_secret", lambda: "unit-test-secret-do-not-deploy")
    oauth.invalidate_session_generation_cache("100")

    now = [1000.0]
    monkeypatch.setattr(oauth.time, "monotonic", lambda: now[0])

    store_gen = [1]
    reads = [0]

    def fake_get_gen(uid: str) -> int:
        reads[0] += 1
        return store_gen[0]

    monkeypatch.setattr(oauth, "get_session_generation", fake_get_gen)

    tok_v1 = oauth._make_session("100", generation=1)
    assert oauth._verify_active_session(tok_v1) == "100"
    assert reads[0] == 1

    # Bump generation externally (e.g., on another pod); cached for <= 60s.
    store_gen[0] = 2
    now[0] += 59.0
    assert oauth._verify_active_session(tok_v1) == "100"
    assert reads[0] == 1

    # Past 60s, cache expires and old token is refused.
    now[0] += 2.0
    assert oauth._verify_active_session(tok_v1) is None
    assert reads[0] == 2


def test_generation_read_fault_refuses_rather_than_admits(monkeypatch):
    import auth.github_oauth as oauth

    monkeypatch.setattr(oauth, "_state_secret", lambda: "unit-test-secret-do-not-deploy")
    oauth.invalidate_session_generation_cache("100")

    def boom(_uid: str) -> int:
        raise psycopg.OperationalError("db down")

    monkeypatch.setattr(oauth, "get_session_generation", boom)
    tok = oauth._make_session("100", generation=1)
    assert oauth._verify_active_session(tok) is None

    # Also verify /api/v1/me refuses as signed out on DB read fault.
    client = TestClient(app)
    client.cookies.set("grug_session", tok)
    resp = client.get("/api/v1/me")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


def test_user_revoke_sessions_endpoint_bumps_generation_and_clears_cookie(monkeypatch):
    import auth.dependencies as deps
    import auth.github_oauth as oauth

    monkeypatch.setattr(oauth, "_state_secret", lambda: "unit-test-secret-do-not-deploy")
    oauth.invalidate_session_generation_cache("100")
    u = _user(role="user", gen=1)
    tok = oauth._make_session("100", generation=1)

    with (
        patch.object(oauth, "_verify_active_session", return_value="100"),
        patch.object(oauth, "get_user", return_value=u),
        patch.object(oauth, "bump_session_generation", return_value=2) as mock_bump,
    ):
        client = TestClient(app)
        client.cookies.set("grug_session", tok)
        resp = client.post("/api/v1/auth/revoke-sessions")

    assert resp.status_code == 200
    assert resp.json() == {"revoked": True, "session_generation": 2}
    mock_bump.assert_called_once_with("100")


def test_admin_revoke_sessions_endpoint_requires_admin_and_bumps_generation():
    import admin as admin_mod
    import auth.dependencies as deps

    admin_user = _user(role="admin", gen=1)
    with (
        patch.object(deps, "verify_active_session", return_value="100"),
        patch.object(deps, "get_user", return_value=admin_user),
        patch.object(admin_mod, "get_user_item", return_value={"login": "bob"}),
        patch.object(admin_mod, "bump_session_generation", return_value=3) as mock_bump,
    ):
        client = TestClient(app)
        client.cookies.set("grug_session", "tok")
        resp = client.post("/api/v1/admin/users/200/revoke-sessions")

    assert resp.status_code == 200
    assert resp.json() == {"github_user_id": "200", "session_generation": 3}
    mock_bump.assert_called_once_with("200")


def test_generation_cache_is_bounded(monkeypatch):
    """A flood of distinct users must not grow the cache without limit."""
    import auth.github_oauth as oauth

    monkeypatch.setattr(oauth, "_SESSION_GEN_CACHE_MAX_ENTRIES", 3)
    oauth._session_gen_cache.clear()
    monkeypatch.setattr(oauth, "get_session_generation", lambda uid: 1)

    for uid in ("1", "2", "3", "4", "5"):
        oauth._cached_session_generation(uid)

    assert len(oauth._session_gen_cache) == 3
    # The oldest entries went first; the newest are still cached.
    assert set(oauth._session_gen_cache) == {"3", "4", "5"}
    oauth._session_gen_cache.clear()


def test_expired_generation_entries_are_dropped_before_live_ones(monkeypatch):
    import auth.github_oauth as oauth

    monkeypatch.setattr(oauth, "_SESSION_GEN_CACHE_MAX_ENTRIES", 2)
    oauth._session_gen_cache.clear()
    monkeypatch.setattr(oauth, "get_session_generation", lambda uid: 1)
    now = [1000.0]
    monkeypatch.setattr(oauth.time, "monotonic", lambda: now[0])

    oauth._cached_session_generation("1")
    now[0] += 30.0
    oauth._cached_session_generation("2")
    now[0] += 40.0  # "1" is now past its 60s life, "2" is not
    oauth._cached_session_generation("3")

    assert set(oauth._session_gen_cache) == {"2", "3"}
    oauth._session_gen_cache.clear()


def test_revoke_then_relogin_through_the_real_store(pg_store, monkeypatch):
    """No mocks on auth or the generation bump: the revoke endpoint runs
    against the real store, the old cookie dies, and a fresh login (the
    callback's upsert plus `_make_session`) is admitted at the new generation."""
    import auth.github_oauth as oauth
    from adapters.pg_user_store import upsert_oauth_user

    monkeypatch.setattr(oauth, "_state_secret", lambda: "unit-test-secret-do-not-deploy")
    oauth._session_gen_cache.clear()

    first = upsert_oauth_user(
        github_user_id="100", login="alice", oauth_access_token="tok",
    )
    old_cookie = oauth._make_session("100", generation=first.session_generation)
    assert oauth._verify_active_session(old_cookie) == "100"

    client = TestClient(app)
    client.cookies.set("grug_session", old_cookie)
    resp = client.post("/api/v1/auth/revoke-sessions")
    assert resp.status_code == 200
    assert resp.json() == {
        "revoked": True, "session_generation": first.session_generation + 1,
    }

    # The old cookie is refused, including by the endpoints themselves.
    assert oauth._verify_active_session(old_cookie) is None
    client.cookies.set("grug_session", old_cookie)
    assert client.get("/api/v1/me").json() == {"authenticated": False}
    assert client.post("/api/v1/auth/revoke-sessions").status_code == 401

    # Logging in again keeps the bumped generation and mints a working cookie.
    second = upsert_oauth_user(
        github_user_id="100", login="alice", oauth_access_token="tok2",
    )
    assert second.session_generation == first.session_generation + 1
    new_cookie = oauth._make_session("100", generation=second.session_generation)
    assert oauth._verify_active_session(new_cookie) == "100"
    assert oauth._verify_active_session(old_cookie) is None
    oauth._session_gen_cache.clear()
