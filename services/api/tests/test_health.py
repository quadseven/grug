"""Health-endpoint tests for grug-api.

Per `feedback_health_endpoint_standard` memory: /livez (alive) +
/readyz (ready), NOT /healthz. /api/v1/health is the build/uptime
probe used by uptime monitors.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_livez_returns_200_with_status_ok() -> None:
    r = client.get("/livez")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "grug-api"


def test_readyz_returns_200_with_status_ready() -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["service"] == "grug-api"


def test_api_v1_health_includes_build_and_uptime() -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "grug-api"
    assert "build" in body
    assert "env" in body
    assert isinstance(body["uptime_seconds"], (int, float))
    assert body["uptime_seconds"] >= 0


def test_no_healthz_endpoint() -> None:
    """Memory feedback_health_endpoint_standard: /healthz is K8s-deprecated.
    grug-api ships /livez + /readyz instead — not /healthz."""
    r = client.get("/healthz")
    assert r.status_code == 404
