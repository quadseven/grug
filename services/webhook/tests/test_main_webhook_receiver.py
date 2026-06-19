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
from unittest.mock import patch

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
        dispatcher,
        "dispatch",
        lambda event, payload, *, delivery_id="": {
            "status": "no_op",
            "reason": "stubbed",
        },
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


def test_unsigned_probe_logs_distinct_event_not_signature_invalid(_client, caplog):
    """An unsigned request (no X-Hub-Signature-256) is an internet probe of the
    public URL, NOT a GitHub delivery being rejected — it must log
    `webhook_unsigned_probe`, never `webhook_signature_invalid` (the monitored
    alert). Keeps the sig-verify monitor precise to real rotated-secret events."""
    import logging

    caplog.set_level(logging.INFO)
    _client.post(
        "/webhook/github",
        content=b'{"action":"opened"}',
        headers={"X-GitHub-Event": "pull_request"},
    )
    msgs = [r.getMessage() for r in caplog.records]
    assert "webhook_unsigned_probe" in msgs
    assert "webhook_signature_invalid" not in msgs


def test_signed_but_invalid_logs_signature_invalid(_client, caplog):
    """A request that carried a signature but failed verification is a real
    rejection (rotated secret / forged delivery) — it must log the monitored
    `webhook_signature_invalid`, not the quiet probe event."""
    import logging

    caplog.set_level(logging.WARNING)
    _client.post(
        "/webhook/github",
        content=b'{"action":"opened"}',
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=000000000000",
        },
    )
    msgs = [r.getMessage() for r in caplog.records]
    assert "webhook_signature_invalid" in msgs


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


def test_dispatched_personas_list_response_returns_200(_client, monkeypatch):
    """#368 live-proof regression: dispatch() returns {status, personas: [...]}
    (LIST-shaped) for pull_request events. The route annotation must accept
    it - a dict[str, str] annotation made FastAPI's response validation 500
    every dispatched PR delivery on the k8s image (the Lambda image's older
    dependency set never enforced it, which hid the bug)."""
    import dispatcher

    monkeypatch.setattr(
        dispatcher,
        "dispatch",
        lambda event, payload, *, delivery_id="": {
            "status": "dispatched",
            "personas": [
                {"persona": "tpm", "result": "pass"},
                {"persona": "code_reviewer", "result": "queued"},
            ],
        },
    )
    body = b'{"action":"synchronize","number":373}'
    r = _client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "k8sproof-regression",
            "X-Hub-Signature-256": _sign(_WEBHOOK_SECRET, body),
        },
    )
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "dispatched"
    assert out["personas"][1] == {"persona": "code_reviewer", "result": "queued"}


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


def test_receiver_threads_delivery_id_into_dispatch(_client):
    """The X-GitHub-Delivery header must be PASSED to dispatch (not just
    echoed in the response) — the async Elder idempotency claim keys on it
    (#272). The response-echo above can't catch a dropped kwarg; this does."""
    import dispatcher

    captured = {}

    def _capture(event, payload, *, delivery_id=""):
        captured["delivery_id"] = delivery_id
        return {"status": "no_op", "reason": "stub"}

    body = b'{"action":"opened"}'
    with patch.object(dispatcher, "dispatch", _capture):
        r = _client.post(
            "/webhook/github",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "deliv-xyz",
                "X-Hub-Signature-256": _sign(_WEBHOOK_SECRET, body),
            },
        )
    assert r.status_code == 200
    assert captured["delivery_id"] == "deliv-xyz"


def test_concurrent_deliveries_do_not_serialize_on_ack_path(monkeypatch):
    """#371: on k8s, uvicorn serves concurrent deliveries on ONE event loop.
    The ACK-path sync calls (SSM secret fetch, dispatch) must be offloaded via
    asyncio.to_thread so two simultaneous deliveries don't serialize - one
    handler's sync call must not block the other's ACK. Proven with a
    max-concurrency counter: if the slow sync call runs on the loop, the two
    deliveries serialize and max concurrency is 1; offloaded to threads they
    overlap and reach 2."""
    import asyncio
    import threading
    import time

    import httpx

    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET_SSM", "/grug/test-webhook-secret")
    monkeypatch.setenv("GRUG_DDB_TABLE", "grug-main-test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import main as webhook_main
    import dispatcher

    state = {"current": 0, "max": 0}
    lock = threading.Lock()

    def slow_secret() -> str:
        # Simulate the blocking SSM round-trip on the ACK path.
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        time.sleep(0.2)
        with lock:
            state["current"] -= 1
        return _WEBHOOK_SECRET

    monkeypatch.setattr(webhook_main, "get_webhook_secret", slow_secret)
    monkeypatch.setattr(
        dispatcher,
        "dispatch",
        lambda event, payload, *, delivery_id="": {"status": "no_op"},
    )

    body = b'{"action":"opened"}'
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": _sign(_WEBHOOK_SECRET, body),
    }

    async def _drive() -> tuple[int, int]:
        transport = httpx.ASGITransport(app=webhook_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            async def _one() -> int:
                r = await c.post("/webhook/github", content=body, headers=headers)
                return r.status_code

            return await asyncio.gather(_one(), _one())

    codes = asyncio.run(_drive())
    assert codes == [200, 200]
    assert state["max"] == 2, (
        "ACK-path sync call serialized the two deliveries on the event loop "
        "(expected concurrent offload via asyncio.to_thread)"
    )
