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
