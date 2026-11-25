"""GZipMiddleware tests for grug-api."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from adapters.user_store import UserIdentity
from auth.dependencies import require_admin
from main import app


def _fake_admin() -> UserIdentity:
    return UserIdentity(
        github_user_id="100",
        login="op",
        role="admin",
        tier="lifetime",
        allowlisted=True,
        created_at="",
        allowlisted_at=None,
        allowlisted_by=None,
    )


def test_gzip_compresses_large_json_responses_and_preserves_cors() -> None:
    rows = [
        {
            "PK": f"USER#{i}",
            "login": f"user-{i}",
            "role": "user",
            "tier": "free",
            "allowlisted": True,
            "created_at": "2026-01-01T00:00:00Z",
            "last_login_at": "2026-01-02T00:00:00Z",
        }
        for i in range(25)
    ]
    app.dependency_overrides[require_admin] = _fake_admin
    try:
        with patch("admin._scan_all", return_value=rows):
            resp = TestClient(app).get(
                "/api/v1/admin/users",
                headers={
                    "Accept-Encoding": "gzip",
                    "Origin": "https://grug.lol",
                },
            )
    finally:
        app.dependency_overrides.pop(require_admin, None)

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in resp.headers.get("vary", "").lower()
    assert resp.headers.get("access-control-allow-origin") == "https://grug.lol"
    assert len(resp.json()["users"]) == 25


def test_gzip_skips_small_responses() -> None:
    resp = TestClient(app).get("/livez", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") is None
    assert resp.json()["status"] == "ok"
