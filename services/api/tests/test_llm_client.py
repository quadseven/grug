"""Tests for the LLM client abstraction (issue #184).

Covers:
- review_diff returns LlmReviewResponse with backend_used + model_name
- Round-robin selection via installation_id % 2 (even → Poolside, odd → OpenRouter)
- 429 retry with backoff on OpenRouter
- Graceful fallback when primary backend errors
- Timeout handling
- OpenAI-compatible request shape (system + user message, JSON response format)
- Empty hunks → no LLM call (cheap return)
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

import llm_client as lc
from llm_client import Backend, Hunk, LlmReviewResponse, review_diff


@pytest.fixture(autouse=True)
def _patch_keys(monkeypatch):
    """Avoid the real SSM round-trip in tests."""
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")


def _hunk(path="src/x.py", body="@@ -1 +1 @@\n-foo\n+bar") -> Hunk:
    return Hunk(path=path, body=body)


def _openai_json_response(findings_json: str) -> dict:
    """OpenAI-compatible chat completion shape both backends return."""
    return {
        "choices": [
            {"message": {"content": findings_json, "role": "assistant"}},
        ],
        "model": "test-model-id",
    }


def test_round_robin_even_installation_picks_poolside() -> None:
    """installation_id % 2 == 0 → Poolside backend."""
    assert lc.select_backend(installation_id=2) == Backend.POOLSIDE
    assert lc.select_backend(installation_id=42) == Backend.POOLSIDE


def test_round_robin_odd_installation_picks_openrouter() -> None:
    assert lc.select_backend(installation_id=1) == Backend.OPENROUTER
    assert lc.select_backend(installation_id=43) == Backend.OPENROUTER


def test_empty_hunks_returns_no_findings_without_llm_call() -> None:
    """Cheap short-circuit — don't burn LLM quota on empty diffs."""
    with patch.object(httpx, "post") as mock_post:
        out = review_diff([], installation_id=1)
    assert out.findings == []
    assert out.backend_used is None
    mock_post.assert_not_called()


def test_review_diff_via_poolside_returns_structured_response() -> None:
    findings_json = '{"findings": [{"path": "src/x.py", "line": 1, "rule": "secret-in-log", "severity": "high"}]}'
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=2)

    assert isinstance(out, LlmReviewResponse)
    assert out.backend_used == Backend.POOLSIDE
    assert out.model_name == "test-model-id"
    assert len(out.findings) == 1
    assert out.findings[0]["rule"] == "secret-in-log"


def test_review_diff_via_openrouter_returns_structured_response() -> None:
    findings_json = '{"findings": []}'
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.backend_used == Backend.OPENROUTER
    assert out.findings == []


def test_429_triggers_retry_with_backoff(monkeypatch) -> None:
    """OpenRouter free tier sends 429 under burst; client retries."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)  # no real sleep
    seq = [
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        httpx.Response(200, json=_openai_json_response('{"findings":[]}')),
    ]
    idx = {"n": 0}

    def staged_post(*args, **kwargs):
        i = idx["n"]
        idx["n"] += 1
        return seq[i]

    with patch.object(httpx, "post", side_effect=staged_post):
        out = review_diff([_hunk()], installation_id=1)

    assert idx["n"] == 3, "should have made 3 attempts (2 retries after 429)"
    assert out.backend_used == Backend.OPENROUTER


def test_primary_failure_falls_back_to_secondary(monkeypatch) -> None:
    """5xx on primary → no per-backend retry (might be a permanent issue);
    fall back to the other backend immediately."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    seq = [
        httpx.Response(500, json={"error": "upstream"}),  # Poolside
        httpx.Response(200, json=_openai_json_response('{"findings": [{"rule": "x", "path": "p", "line": 1, "severity": "low"}]}')),  # OpenRouter
    ]
    idx = {"n": 0}

    def staged_post(*args, **kwargs):
        i = idx["n"]
        idx["n"] += 1
        return seq[i]

    with patch.object(httpx, "post", side_effect=staged_post):
        out = review_diff([_hunk()], installation_id=2)  # even → Poolside first

    assert out.backend_used == Backend.OPENROUTER
    assert len(out.findings) == 1


def test_both_backends_fail_returns_empty_response() -> None:
    """When both Poolside AND OpenRouter fail, surface a structured empty
    response with `backend_used=None` instead of crashing — the caller
    posts an advisory check-run that says "skipped" rather than 500ing
    the webhook handler."""
    response = httpx.Response(500, json={"error": "down"})

    with patch.object(httpx, "post", return_value=response), \
         patch.object(lc, "_RETRY_SLEEP", lambda s: None):
        out = review_diff([_hunk()], installation_id=1)

    assert out.findings == []
    assert out.backend_used is None
    assert "both backends failed" in out.error.lower()


def test_timeout_treated_as_failure(monkeypatch) -> None:
    """A timeout (httpx.ReadTimeout) on the primary should trigger
    fallback to the other backend, not crash the webhook."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    call_log: list = []
    success = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    def staged(url, *args, **kwargs):
        call_log.append(url)
        # First 3 calls (Poolside primary + retries) raise timeout;
        # subsequent fallback call to OpenRouter succeeds.
        if "openrouter" in url:
            return success
        raise httpx.ReadTimeout("timeout")

    with patch.object(httpx, "post", side_effect=staged):
        out = review_diff([_hunk()], installation_id=2)

    assert out.backend_used == Backend.OPENROUTER
    assert any("openrouter" in u for u in call_log)


def test_request_uses_openai_chat_completions_shape() -> None:
    captured: list = []

    def capture(url, *, json, headers, timeout):
        captured.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    with patch.object(httpx, "post", side_effect=capture):
        review_diff([_hunk()], installation_id=1)

    assert len(captured) == 1
    body = captured[0]["json"]
    assert "model" in body
    assert isinstance(body["messages"], list)
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    # OpenAI-compatible JSON-mode hint to coerce structured response
    assert body.get("response_format") == {"type": "json_object"}
    # Authorization header carries the loaded key.
    assert captured[0]["headers"]["Authorization"].startswith("Bearer ")
    # 30s timeout per the existing Poolside convention.
    assert captured[0]["timeout"] == 30


def test_malformed_llm_json_returns_empty_findings_not_crash() -> None:
    """LLM occasionally returns prose around the JSON or just refuses
    to comply. Don't crash the webhook on a parse error — degrade to
    'no findings' with a logged warning."""
    response = httpx.Response(200, json=_openai_json_response("sorry, I cannot do that"))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.findings == []
    assert out.backend_used == Backend.OPENROUTER
    assert "parse" in out.error.lower()
