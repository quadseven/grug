"""Tests for auth.dependencies — get_current_user / require_authenticated /
require_admin gates.

Auth gates run on every API request; bugs here shadow OAuth + admin
controls. Coverage:

- get_current_user: empty cookie → None
- get_current_user: invalid cookie → None
- get_current_user: valid cookie + missing user row → None
- get_current_user: valid cookie + present user → User
- require_authenticated: None → 401
- require_authenticated: User → User passthrough
- require_admin: anonymous → 401
- require_admin: non-admin user → 403
- require_admin: admin user → User passthrough
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

import auth.dependencies as deps
from adapters.user_store import User


def _user(*, role="user"):
    return User(
        github_user_id="100",
        login="evan",
        role=role,
        tier="free",
        allowlisted=True,
        oauth_access_token="x",
        oauth_refresh_token=None,
        created_at="",
    )


def test_get_current_user_empty_cookie_returns_none():
    with patch.object(deps, "_verify_session", return_value=None):
        assert deps.get_current_user("") is None


def test_get_current_user_invalid_cookie_returns_none():
    with patch.object(deps, "_verify_session", return_value=None):
        assert deps.get_current_user("not-a-valid-session") is None


def test_get_current_user_missing_user_row_returns_none():
    with patch.object(deps, "_verify_session", return_value="100"):
        with patch.object(deps, "get_user", return_value=None):
            assert deps.get_current_user("valid-session") is None


def test_get_current_user_valid_returns_user():
    u = _user()
    with patch.object(deps, "_verify_session", return_value="100"):
        with patch.object(deps, "get_user", return_value=u):
            assert deps.get_current_user("valid-session") is u


def test_require_authenticated_anonymous_raises_401():
    with patch.object(deps, "_verify_session", return_value=None):
        with pytest.raises(HTTPException) as exc:
            deps.require_authenticated("")
    assert exc.value.status_code == 401
    assert "not authenticated" in exc.value.detail


def test_require_authenticated_passes_user_through():
    u = _user()
    with patch.object(deps, "_verify_session", return_value="100"):
        with patch.object(deps, "get_user", return_value=u):
            assert deps.require_authenticated("valid") is u


def test_require_admin_anonymous_raises_401():
    with patch.object(deps, "_verify_session", return_value=None):
        with pytest.raises(HTTPException) as exc:
            deps.require_admin("")
    # 401 not 403 — anonymous gets the auth gate first
    assert exc.value.status_code == 401


def test_require_admin_non_admin_raises_403():
    u = _user(role="user")
    with patch.object(deps, "_verify_session", return_value="100"):
        with patch.object(deps, "get_user", return_value=u):
            with pytest.raises(HTTPException) as exc:
                deps.require_admin("valid")
    assert exc.value.status_code == 403
    assert "admin role required" in exc.value.detail


def test_require_admin_admin_user_passes_through():
    u = _user(role="admin")
    with patch.object(deps, "_verify_session", return_value="100"):
        with patch.object(deps, "get_user", return_value=u):
            assert deps.require_admin("valid") is u
