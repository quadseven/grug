"""Tests for the shared LLM-attempt seam (#660).

`_run_llm_attempt` owns the envelope (span-open + `_call_backend` +
error-classify + body-reparse + `_extract_usage_metrics` +
`_llmobs_annotate`) exactly once; `_run_review_arm` and the SaaS overload
fallback loop are thin callers. Byte-preserving: same span name, same
annotate-on-every-exit metadata, same `_ArmOutcome` values.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import httpx
import llm_client as lc
from llm_client import Backend, _ArmOutcome, _run_llm_attempt


def _config():
    return SimpleNamespace(model="test-model", api_key="k", url="http://x.test")


def _ok_response():
    body = {
        "choices": [{"message": {"content": json.dumps({"findings": []})}}],
        "model": "test-model-id",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    return httpx.Response(200, json=body)


def test_seam_exists_with_expected_shape():
    sig = inspect.signature(_run_llm_attempt)
    params = list(sig.parameters)
    assert "backend" in params
    assert "config" in params
    assert "messages" in params
    assert "cancel_event" in params


def test_seam_returns_findings_on_success(monkeypatch):
    monkeypatch.setattr(lc, "_call_backend", lambda *a, **k: _ok_response())
    outcome = _run_llm_attempt(
        backend=Backend.OPENROUTER,
        config=_config(),
        messages=[{"role": "user", "content": "hi"}],
        variant="default",
        pr_tags={},
    )
    assert outcome.error_kind is None
    assert outcome.model == "test-model-id"
    assert outcome.status_code == 200
    assert outcome.raw_response is not None


def test_seam_classifies_transport_error(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(lc, "_call_backend", boom)
    outcome = _run_llm_attempt(
        backend=Backend.POOLSIDE,
        config=_config(),
        messages=[{"role": "user", "content": "hi"}],
        variant="default",
        pr_tags={},
    )
    assert outcome.error_kind == "transport"
    assert outcome.findings == ()


def test_seam_classifies_config_error(monkeypatch):
    def boom(*a, **k):
        raise lc._BackendConfigError("no key")

    monkeypatch.setattr(lc, "_call_backend", boom)
    outcome = _run_llm_attempt(
        backend=Backend.OPENROUTER,
        config=_config(),
        messages=[{"role": "user", "content": "hi"}],
        variant="default",
        pr_tags={},
    )
    assert outcome.error_kind == "config"


def test_run_review_arm_delegates_to_seam(monkeypatch):
    """_run_review_arm resolves config then calls the seam exactly once."""
    calls = []

    def fake_seam(**kwargs):
        calls.append(kwargs)
        return lc.AttemptOutcome(
            backend=kwargs["backend"],
            model="m",
            findings=(),
            content="",
            error_kind=None,
            error_text=None,
            status_code=200,
            finish_reason="",
            usage_metrics={},
            span_context=None,
            raw_response=_ok_response(),
        )

    monkeypatch.setattr(lc, "_run_llm_attempt", fake_seam)
    monkeypatch.setattr(lc, "_review_backend_config", lambda backend: _config())
    outcome = lc._run_review_arm(
        Backend.CAVE, [{"role": "user", "content": "hi"}], "default", {}
    )
    assert len(calls) == 1
    assert calls[0]["backend"] is Backend.CAVE
    assert isinstance(outcome, _ArmOutcome)
    assert outcome.kind == "success"
