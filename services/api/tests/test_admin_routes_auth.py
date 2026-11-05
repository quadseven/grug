"""Every admin route requires the admin role (#1636).

The handler tests in test_admin.py call route functions with a
constructed identity, so dropping a `Depends(require_admin)` from a
route would not fail them. These tests read the wiring itself, the same
way test_learnings_routes.test_every_route_is_admin_only does, and then
drive each route through the real app with no session and with a
non-admin session.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests._route_auth import requests_for, routes_of, top_level_deps


@pytest.fixture
def client(monkeypatch):
    # The CF origin boundary has its own suite; open it so the request
    # reaches the route's own auth dependency.
    monkeypatch.setenv("GRUG_CF_AUTH_FAIL_OPEN", "1")
    import main
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def _user(role):
    from adapters.user_store import UserIdentity

    return UserIdentity(
        github_user_id="7", login="someone", role=role, tier="free",
        allowlisted=True, created_at="", allowlisted_at=None, allowlisted_by=None,
    )


def test_every_route_is_admin_only():
    import admin
    from auth.dependencies import require_admin

    for route in routes_of(admin.router):
        assert require_admin in top_level_deps(route), route.path


def test_every_route_answers_401_without_a_session(client):
    import admin

    for route in routes_of(admin.router):
        for method, path in requests_for(route):
            resp = client.request(method, path, json={})
            assert resp.status_code == 401, (method, path, resp.status_code)


def test_every_route_answers_403_for_a_non_admin(client):
    import admin

    with patch("auth.dependencies._verify_session", return_value="7"), \
            patch("auth.dependencies.get_user", return_value=_user("user")):
        client.cookies.set("grug_session", "s")
        for route in routes_of(admin.router):
            for method, path in requests_for(route):
                resp = client.request(method, path, json={})
                assert resp.status_code == 403, (method, path, resp.status_code)
