"""TestClient-driven tests for receive_github_webhook.

PR #99 added the JSON-decode-after-HMAC 400 branch but had no test —
pr-test-analyzer HIGH gap #2.

Uses FastAPI TestClient + signs payloads with the real verify_signature
HMAC primitive so the HMAC gate runs end-to-end without mocking it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient


_WEBHOOK_SECRET = "test-webhook-secret-v1"


def _sign(secret: str, body: bytes) -> str:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


@pytest.fixture
def _client(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET_SSM", "/grug/test-webhook-secret")
    monkeypatch.setenv("GRUG_DDB_TABLE", "grug-main-test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import main as webhook_main
    monkeypatch.setattr(webhook_main, "get_webhook_secret", lambda: _WEBHOOK_SECRET)
    # Stub dispatcher so we don't need DDB/KMS for the webhook-receiver tests
    import dispatcher
    monkeypatch.setattr(
        dispatcher, "dispatch",
        lambda event, payload: {"status": "no_op", "reason": "stubbed"},
    )
    return TestClient(webhook_main.app)


def test_unsigned_post_returns_401(_client):
    r = _client.post(
        "/webhook/github",
        content=b'{"action":"opened"}',
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert r.status_code == 401


def test_bad_signature_returns_401(_client):
    body = b'{"action":"opened"}'
    r = _client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=000000000000",
        },
    )
    assert r.status_code == 401


def test_signed_non_json_body_returns_400(_client):
    """silent-failure-hunter P1 #1: body that passes HMAC but fails
    JSON decode must 400 (not 200 'skip'), so DD alarms trigger."""
    body = b"not json at all { broken"
    r = _client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(_WEBHOOK_SECRET, body),
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "body_not_json"


def test_signed_valid_json_dispatches(_client):
    body = b'{"action":"opened","number":1}'
    r = _client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "deadbeef-1234",
            "X-Hub-Signature-256": _sign(_WEBHOOK_SECRET, body),
        },
    )
    assert r.status_code == 200
    body_json = r.json()
    assert body_json["delivery_id"] == "deadbeef-1234"
    assert body_json["status"] == "no_op"
    assert body_json["reason"] == "stubbed"
