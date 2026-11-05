"""Every installations route requires an authenticated session (#1636).

The handler tests call route functions with a constructed identity, so
dropping a `Depends(require_authenticated)` from a route would not fail
them. These tests read the wiring itself, the same way
test_learnings_routes.test_every_route_is_admin_only does, and then
drive each route through the real app with no session.
"""

from __future__ import annotations

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


def test_every_route_requires_authentication():
    import installations
    from auth.dependencies import require_admin, require_authenticated

    for route in routes_of(installations.router):
        deps = top_level_deps(route)
        assert require_authenticated in deps or require_admin in deps, route.path


def test_every_route_answers_401_without_a_session(client):
    import installations

    for route in routes_of(installations.router):
        for method, path in requests_for(route):
            resp = client.request(method, path, json={})
            assert resp.status_code == 401, (method, path, resp.status_code)
