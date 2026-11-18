"""Characterize the review ladder at the provider wire."""

import json

import httpx
import pytest

import provider_wire
from llm_client import Backend, Hunk, review_diff
from review_evidence import assemble_evidence


@pytest.mark.parametrize(
    ("priority", "public", "cave_answers", "cap", "expected_routes", "kind", "backend"),
    [
        ("cave", False, True, 1, ["cave"], "reviewed", Backend.CAVE),
        ("cave", False, False, 1, ["cave", "subscription"], "reviewed", Backend.SUBSCRIPTION),
        ("cave", False, False, 0, ["cave"], "subscription_limit_reached", None),
        ("subscription", True, True, 1, ["subscription"], "reviewed", Backend.SUBSCRIPTION),
        ("subscription", False, True, 1, ["cave"], "reviewed", Backend.CAVE),
    ],
)
def test_ladder_routes_through_provider_wire(
    monkeypatch, priority, public, cave_answers, cap, expected_routes, kind, backend,
):
    base = "https://cave.test"
    subscription = f"{base}/subscription/v1/chat/completions"
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", base)
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", priority)
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    monkeypatch.setenv(
        "GRUG_DISABLED_BACKENDS", "opencode-go,poolside,openrouter,free-pool,cave-reasoner",
    )
    monkeypatch.setenv("GRUG_SUBSCRIPTION_URL", subscription)
    monkeypatch.setenv("GRUG_SUBSCRIPTION_DAILY_CAP", str(cap))

    routes = []
    endpoints = {f"{base}/v1/chat/completions": "cave", subscription: "subscription"}
    finding = {
        "path": "src/x.py", "line": 1, "rule": "unhandled-error",
        "severity": "high", "message": "Check the return value.",
    }
    content = json.dumps({"findings": [finding]})

    def fake_post(url, *, json, headers, timeout):
        route = endpoints[url]
        routes.append(route)
        if route == "cave" and not cave_answers:
            raise httpx.ConnectError("offline")
        assert headers["X-Spark-Data-Class"] == ("public" if public else "private")
        assert "messages" in json
        return httpx.Response(200, json={
            "model": "served-model",
            "choices": [{"message": {"content": content}}],
        })

    monkeypatch.setattr(provider_wire, "post", fake_post)
    evidence = assemble_evidence(
        [Hunk(path="src/x.py", body="@@ -1 +1 @@\n-old\n+new")],
        pr_context={"repo_private": not public},
    )
    result = review_diff(evidence, installation_id=1)

    assert routes == expected_routes
    assert result.kind == kind
    assert result.backend_used == backend
    assert len(result.findings) == (1 if kind == "reviewed" else 0)
