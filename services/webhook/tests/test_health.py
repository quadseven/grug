"""Health-endpoint tests for grug-webhook.

Per `feedback_health_endpoint_standard` memory: /livez (alive) +
/readyz (ready), NOT /healthz. Mirrors services/api/tests/test_health.py
for parity.
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
    assert body["service"] == "grug-webhook"


def test_readyz_returns_200_with_status_ready() -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["service"] == "grug-webhook"


def test_no_healthz_endpoint() -> None:
    """Memory feedback_health_endpoint_standard: /healthz is K8s-deprecated.
    grug-webhook ships /livez + /readyz instead."""
    r = client.get("/healthz")
    assert r.status_code == 404


def test_livez_does_no_io():
    """Liveness must be cheap — no DDB, KMS, or HTTPX call. If it ever
    starts depending on downstream, move it to /readyz semantics."""
    import inspect
    import main as webhook_main
    src = inspect.getsource(webhook_main.livez)
    forbidden = ("get_item", "decrypt", "httpx.", "_table.", "boto3.")
    assert not any(token in src for token in forbidden), \
        f"livez must not do IO; found one of {forbidden} in source"
