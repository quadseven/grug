"""grug#1191: the optional vLLM deep arm - shadow, serve, one-switch rollback.

The arm is routed by GRUG_DEEP_ARM_MODE. These tests pin the three promises
the ticket makes: shadow output never reaches a PR, the arm is only called
when the vLLM server reports the model resident (no cold load on the review
path), and `off` alone restores the current reasoner.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml

import deep_shadow
import llm_client as lc
from elder_eval import gate
from llm_client import Backend, Hunk
from review_evidence import assemble_evidence
from sast_benchmark import backends

_GATEWAY = "http://cave.test"
_VLLM = "http://deep-vllm.test"
_VLLM_MODEL = "org/deep-model-fp8"


class _InlinePool:
    """Runs the shadow job on the calling thread so its effects are visible
    to the assertions that follow."""

    def submit(self, fn):
        future: Future = Future()
        future.set_result(fn())
        return future


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", _GATEWAY)
    monkeypatch.delenv("GRUG_REVIEW_BACKEND_PRIORITY", raising=False)
    monkeypatch.setenv("GRUG_DEEP_SHADOW_URL", _VLLM)
    monkeypatch.setenv("GRUG_DEEP_SHADOW_MODEL", _VLLM_MODEL)
    monkeypatch.delenv("GRUG_DEEP_ARM_MODE", raising=False)
    monkeypatch.setattr(deep_shadow, "_POOL", _InlinePool())
    monkeypatch.setattr(deep_shadow, "_residency_cache", {})
    monkeypatch.setattr(deep_shadow, "_busy", threading.BoundedSemaphore(1))


def _hunk() -> Hunk:
    return Hunk(path="src/x.py", body="@@ -1 +1 @@\n-foo\n+bar")


def _completion(findings: list[dict], model: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": json.dumps({"findings": findings})}}],
        "model": model,
    }


_GATEWAY_FINDING = {
    "path": "src/x.py", "line": 1, "rule": "gateway-rule",
    "severity": "high", "message": "found by the current reasoner",
}
_VLLM_FINDING = {
    "path": "src/x.py", "line": 1, "rule": "vllm-rule",
    "severity": "high", "message": "found by the vLLM arm",
}


def _models(owned_by: str = "vllm", model: str = _VLLM_MODEL) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": [{"id": model, "owned_by": owned_by}]},
        request=httpx.Request("GET", f"{_VLLM}/v1/models"),
    )


class _Fake:
    """Routes chat completions by host so each test can see which arm ran."""

    def __init__(self, models: httpx.Response, vllm_status: int = 200):
        self.models = models
        self.vllm_status = vllm_status
        self.posts: list[str] = []
        self.gets: list[str] = []

    def post(self, url, **_kw):
        self.posts.append(url)
        if url.startswith(_VLLM):
            if self.vllm_status != 200:
                return httpx.Response(self.vllm_status, json={"error": "boom"})
            return httpx.Response(200, json=_completion([_VLLM_FINDING], _VLLM_MODEL))
        return httpx.Response(200, json=_completion([_GATEWAY_FINDING], "gateway-model"))

    def get(self, url, **_kw):
        self.gets.append(url)
        return self.models


def _review(fake: _Fake):
    with patch.object(httpx, "post", side_effect=fake.post), patch.object(
        httpx, "get", side_effect=fake.get,
    ):
        return lc.review_reasoner_diff(assemble_evidence([_hunk()]), installation_id=1)


def _vllm_posts(fake: _Fake) -> list[str]:
    return [u for u in fake.posts if u.startswith(_VLLM)]


def _rules(out) -> set[str]:
    return {f.rule for f in out.findings}


def test_off_is_the_default_and_never_touches_vllm() -> None:
    fake = _Fake(_models())
    out = _review(fake)
    assert _rules(out) == {"gateway-rule"}
    assert _vllm_posts(fake) == [] and fake.gets == []


def test_shadow_publishes_only_the_current_reasoner(monkeypatch, caplog) -> None:
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", "shadow")
    fake = _Fake(_models())
    with caplog.at_level(logging.INFO):
        out = _review(fake)
    assert _rules(out) == {"gateway-rule"}
    assert out.backends_used == (Backend.CAVE_REASONER,)
    assert out.models_used == ("gateway-model",)
    assert _vllm_posts(fake) == [f"{_VLLM}/v1/chat/completions"]
    (record,) = [r for r in caplog.records if r.message == "deep_shadow_compared"]
    assert record.shadow_count == 1 and record.production_count == 1
    assert record.overlap == 1 and record.shadow_model == _VLLM_MODEL


def test_shadow_skips_a_model_vllm_does_not_report_resident(monkeypatch, caplog) -> None:
    # An Ollama-style front lists models it would load on demand; calling it
    # would be the cold load the ticket forbids.
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", "shadow")
    fake = _Fake(_models(owned_by="library"))
    with caplog.at_level(logging.WARNING):
        out = _review(fake)
    assert _rules(out) == {"gateway-rule"}
    assert _vllm_posts(fake) == []
    assert any(r.message == "deep_vllm_not_resident" for r in caplog.records)


def test_shadow_skips_when_the_worker_is_still_busy(monkeypatch, caplog) -> None:
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", "shadow")
    assert deep_shadow._busy.acquire(blocking=False)
    fake = _Fake(_models())
    with caplog.at_level(logging.INFO):
        out = _review(fake)
    assert _rules(out) == {"gateway-rule"}
    assert _vllm_posts(fake) == []
    assert any(r.message == "deep_shadow_skipped_busy" for r in caplog.records)


def test_shadow_job_failure_is_logged_not_raised(monkeypatch, caplog) -> None:
    monkeypatch.setattr(deep_shadow, "_POOL", deep_shadow.ThreadPoolExecutor(max_workers=1))

    def explode() -> None:
        raise RuntimeError("shadow blew up")

    with caplog.at_level(logging.ERROR):
        future = deep_shadow.submit(explode)
        assert future is not None
        with pytest.raises(RuntimeError):
            future.result(timeout=5)
        deep_shadow._POOL.shutdown(wait=True)
    assert any(r.message == "deep_shadow_failed" for r in caplog.records)
    # The slot is released, so the next review can shadow again.
    assert deep_shadow._busy.acquire(blocking=False)
    deep_shadow._busy.release()


def test_serve_answers_from_the_resident_vllm_arm(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", "serve")
    fake = _Fake(_models())
    out = _review(fake)
    assert _rules(out) == {"vllm-rule"}
    assert out.models_used == (_VLLM_MODEL,)
    assert [u for u in fake.posts if u.startswith(_GATEWAY)] == []


def test_serve_falls_back_to_the_current_reasoner_when_not_resident(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", "serve")
    fake = _Fake(_models(model="some-other-model"))
    out = _review(fake)
    assert _rules(out) == {"gateway-rule"}
    assert _vllm_posts(fake) == []


def test_serve_falls_back_to_the_current_reasoner_when_vllm_fails(monkeypatch, caplog) -> None:
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", "serve")
    fake = _Fake(_models(), vllm_status=500)
    with caplog.at_level(logging.WARNING):
        out = _review(fake)
    assert _rules(out) == {"gateway-rule"}
    assert any(r.message == "deep_vllm_failed_falling_back" for r in caplog.records)


def test_rollback_is_one_switch(monkeypatch) -> None:
    # Everything else about the vLLM arm stays configured; only the mode moves.
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", "serve")
    assert _rules(_review(_Fake(_models()))) == {"vllm-rule"}
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", "off")
    fake = _Fake(_models())
    assert _rules(_review(fake)) == {"gateway-rule"}
    assert _vllm_posts(fake) == []


def test_residency_probe_is_cached_between_reviews(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", "serve")
    fake = _Fake(_models())
    _review(fake)
    _review(fake)
    assert fake.gets == [f"{_VLLM}/v1/models"]


def test_residency_probe_transport_error_reads_not_resident(caplog) -> None:
    arm = deep_shadow.DeepArm(mode="serve", base_url=_VLLM, model=_VLLM_MODEL)
    with patch.object(httpx, "get", side_effect=httpx.ConnectError("refused")), caplog.at_level(
        logging.WARNING,
    ):
        assert deep_shadow.is_resident(arm) is False
    assert any(r.message == "deep_vllm_probe_failed" for r in caplog.records)


@pytest.mark.parametrize("mode", ["shadow", "serve"])
def test_a_mode_without_its_endpoint_resolves_to_off(monkeypatch, caplog, mode) -> None:
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", mode)
    monkeypatch.delenv("GRUG_DEEP_SHADOW_URL")
    with caplog.at_level(logging.ERROR):
        assert deep_shadow.resolve_deep_arm() is None
    assert any(r.message == "deep_arm_unconfigured" for r in caplog.records)


def test_an_unknown_mode_resolves_to_off(monkeypatch, caplog) -> None:
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", "always")
    with caplog.at_level(logging.WARNING):
        assert deep_shadow.resolve_deep_arm() is None
    assert any(r.message == "deep_arm_mode_unknown" for r in caplog.records)


# --- the eval gate (cutover to `serve`) ------------------------------------

_K8S = Path(__file__).resolve().parents[3] / "k8s"
_REVIEW_DEPLOYMENTS = ("consumer-deployment.yaml", "webhook-deployment.yaml")


def _entry(model: str, catch: float, noise: float, cases: int = 17, errored=()) -> dict:
    return {
        "model": model,
        "served_models": {model: cases},
        "overall_catch": catch,
        "noise_rate": noise,
        "cases_scored": cases,
        "errored_cases": list(errored),
    }


def _baseline(**backends: dict) -> dict:
    return {"prompt_sha": "x", "backends": backends}


_CAVE = _entry("qwen3-coder-next:q8_0", catch=0.17, noise=0.0)


def test_gate_passes_a_candidate_no_worse_than_the_incumbent() -> None:
    baseline = _baseline(cave=_CAVE, **{"deep-shadow": _entry(_VLLM_MODEL, 0.25, 0.0)})
    assert gate.deep_cutover_regressions(baseline, _VLLM_MODEL) == []


def test_gate_refuses_without_a_recorded_candidate() -> None:
    (reason,) = gate.deep_cutover_regressions(_baseline(cave=_CAVE), _VLLM_MODEL)
    assert "deep-shadow" in reason


@pytest.mark.parametrize(
    ("candidate", "needle"),
    [
        (_entry("another-model", 0.25, 0.0), "measured another-model"),
        (_entry(_VLLM_MODEL, 0.10, 0.0), "overall_catch"),
        (_entry(_VLLM_MODEL, 0.25, 0.20), "noise_rate"),
        (_entry(_VLLM_MODEL, 0.25, 0.0, cases=9), "cases_scored"),
        (_entry(_VLLM_MODEL, 0.25, 0.0, errored=("case-1",)), "errored"),
    ],
)
def test_gate_refuses_a_candidate_that_regresses(candidate, needle) -> None:
    baseline = _baseline(cave=_CAVE, **{"deep-shadow": candidate})
    reasons = gate.deep_cutover_regressions(baseline, _VLLM_MODEL)
    assert any(needle in r for r in reasons), reasons


def _manifest_env(name: str) -> dict[str, str | None]:
    docs = [d for d in yaml.safe_load_all((_K8S / name).read_text()) if d]
    (dep,) = [d for d in docs if d.get("kind") == "Deployment"]
    (container,) = [c for c in dep["spec"]["template"]["spec"]["containers"] if c.get("env")][:1]
    return {e["name"]: e.get("value") for e in container["env"]}


@pytest.mark.parametrize("name", _REVIEW_DEPLOYMENTS)
def test_a_committed_serve_mode_has_passed_the_eval_gate(name) -> None:
    env = _manifest_env(name)
    mode = (env.get("GRUG_DEEP_ARM_MODE") or "off").strip().lower()
    assert mode in {"off", "shadow", "serve"}, mode
    if mode == "serve":
        regressions = gate.deep_cutover_regressions(
            gate.load_baseline(), env.get("GRUG_DEEP_SHADOW_MODEL") or "",
        )
        assert regressions == [], regressions


def test_deep_arm_settings_match_across_review_deployments() -> None:
    consumer, webhook = (_manifest_env(n) for n in _REVIEW_DEPLOYMENTS)
    for key in ("GRUG_DEEP_ARM_MODE", "GRUG_DEEP_SHADOW_URL", "GRUG_DEEP_SHADOW_MODEL"):
        assert consumer.get(key) == webhook.get(key), key


def test_the_deep_shadow_bench_backend_sends_the_production_body(monkeypatch) -> None:
    for var in ("OPENROUTER_KEY", "POOLSIDE_KEY", "CAVE_URL"):
        monkeypatch.delenv(f"GRUG_BENCH_{var}", raising=False)
    monkeypatch.setenv("GRUG_BENCH_DEEP_SHADOW_URL", f"{_VLLM}/v1/chat/completions")
    monkeypatch.setenv("GRUG_BENCH_DEEP_SHADOW_MODEL", _VLLM_MODEL)
    (bench,) = backends.configured_backends()
    assert bench.name == gate.DEEP_CANDIDATE
    assert bench.model == _VLLM_MODEL
    assert bench.extra_body == lc.DEEP_VLLM_EXTRA_BODY


def test_a_cancelled_review_is_not_shadowed(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_DEEP_ARM_MODE", "shadow")
    cancelled = threading.Event()
    cancelled.set()
    fake = _Fake(_models())
    with patch.object(httpx, "post", side_effect=fake.post), patch.object(
        httpx, "get", side_effect=fake.get,
    ):
        lc.review_reasoner_diff(assemble_evidence([_hunk()]), installation_id=1, cancel_event=cancelled)
    assert _vllm_posts(fake) == []
