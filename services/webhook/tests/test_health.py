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


def test_readyz_returns_200_with_status_ready(monkeypatch) -> None:
    # /readyz is dependency-aware (#404): 200 only when SSM/KMS + Postgres are
    # reachable. Mock the dep check to the reachable case here; the
    # 503-on-dependency-down path is covered in test_readiness.py.
    import readiness
    monkeypatch.setattr(
        readiness, "check_readiness",
        lambda: readiness.ReadinessReport(ready=True, deps={"ssm_kms": True, "postgres": True}),
    )
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


def test_livez_does_no_io(monkeypatch):
    """Liveness must be cheap — no DDB, KMS, or HTTPX call. If it ever
    starts depending on downstream, move it to /readyz semantics.

    Runtime sentinel: patch every potential IO surface with a noisy
    mock that records calls. After hitting /livez, assert zero calls
    to ANY of them. Catches more than the prior source-string scan
    (helper functions, renamed variables, future deps)."""
    from unittest.mock import MagicMock
    import boto3
    import httpx

    boto3_client_calls: list = []
    boto3_resource_calls: list = []
    httpx_calls: list = []

    monkeypatch.setattr(boto3, "client", MagicMock(side_effect=lambda *a, **k: boto3_client_calls.append((a, k)) or MagicMock()))
    monkeypatch.setattr(boto3, "resource", MagicMock(side_effect=lambda *a, **k: boto3_resource_calls.append((a, k)) or MagicMock()))
    for verb in ("get", "post", "put", "delete", "patch"):
        monkeypatch.setattr(httpx, verb, MagicMock(side_effect=lambda *a, **k: httpx_calls.append((verb, a, k)) or MagicMock()))

    r = client.get("/livez")
    assert r.status_code == 200
    assert boto3_client_calls == [], "livez triggered boto3.client()"
    assert boto3_resource_calls == [], "livez triggered boto3.resource()"
    assert httpx_calls == [], f"livez triggered httpx call: {httpx_calls}"
