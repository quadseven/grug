"""CI-safe tests for review_latency pure core (#648)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import httpx
import pytest

from review_latency.fixtures import default_fixtures
from review_latency.scoring import (
    TrialResult,
    percentile,
    summarize_trials,
)


def test_default_fixtures_build_elder_shaped_prompts():
    fixtures = default_fixtures()
    names = {f.name for f in fixtures}
    assert names == {"small", "medium", "large"}
    for f in fixtures:
        assert f.prompt_chars > 500, f"{f.name} prompt too small for latency stress"
        assert f.added_lines > 0
        assert any(m["role"] == "system" for m in f.messages)
        assert any(m["role"] == "user" for m in f.messages)
    by_name = {f.name: f for f in fixtures}
    assert by_name["small"].prompt_chars < by_name["medium"].prompt_chars
    assert by_name["medium"].prompt_chars < by_name["large"].prompt_chars


def test_percentile_nearest_rank():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(vals, 50) == 3.0
    assert percentile(vals, 95) == 5.0
    assert percentile([], 50) is None


def test_summarize_trials_groups_and_p50():
    trials = [
        TrialResult(
            concurrency=1, fixture="small", backend="cave",
            ttft_s=0.1, complete_s=1.0, parse_ok=True, errored=False,
            prompt_chars=100, response_chars=50, completion_tokens=10,
        ),
        TrialResult(
            concurrency=1, fixture="small", backend="cave",
            ttft_s=0.2, complete_s=3.0, parse_ok=True, errored=False,
            prompt_chars=100, response_chars=50, completion_tokens=10,
        ),
        TrialResult(
            concurrency=1, fixture="small", backend="cave",
            ttft_s=None, complete_s=2.0, parse_ok=True, errored=False,
            prompt_chars=100, response_chars=50, completion_tokens=10,
        ),
        TrialResult(
            concurrency=2, fixture="medium", backend="cave",
            ttft_s=0.5, complete_s=4.0, parse_ok=False, errored=False,
            prompt_chars=200, response_chars=80, completion_tokens=20,
        ),
        TrialResult(
            concurrency=2, fixture="medium", backend="cave",
            ttft_s=None, complete_s=9.0, parse_ok=False, errored=True,
            prompt_chars=200, response_chars=0, completion_tokens=None,
        ),
    ]
    # Cell wall for C=1 is 2.0s (not sum of 1+3+2); C=2 is 4.0s.
    walls = {("cave", 1): 2.0, ("cave", 2): 4.0}
    report = summarize_trials(trials, cell_wall_s=walls)
    assert len(report.slices) == 2
    c1 = next(s for s in report.slices if s.concurrency == 1)
    assert c1.fixture == "small"
    assert c1.n == 3
    assert c1.errors == 0
    assert c1.p50_complete_s == 2.0
    assert c1.p95_complete_s == 3.0
    assert c1.p50_ttft_s == 0.1  # nearest-rank of [0.1, 0.2]
    assert c1.p95_ttft_s == 0.2
    # 30 tokens / 2.0s wall
    assert c1.aggregate_tokens_per_s == 15.0
    # 150 chars / 2.0s
    assert c1.aggregate_chars_per_s == 75.0
    c2 = next(s for s in report.slices if s.concurrency == 2)
    assert c2.fixture == "medium"
    assert c2.errors == 1
    assert c2.parse_failures == 1
    md = report.as_markdown()
    assert "p50 complete" in md
    assert "cave" in md
    assert "small" in md


def test_main_exits_2_without_backends(monkeypatch):
    from review_latency import __main__ as main_mod

    monkeypatch.delenv("GRUG_BENCH_CAVE_URL", raising=False)
    monkeypatch.delenv("GRUG_BENCH_CAVE_MODEL", raising=False)
    monkeypatch.delenv("GRUG_BENCH_REASONER_URL", raising=False)
    assert main_mod.main(["--levels", "1"]) == 2


def test_llmobs_export_noop_when_disabled(monkeypatch):
    """Default (no env) must not require ddtrace or ship anything."""
    from review_latency import llmobs_export as le
    from sast_benchmark.backends import BenchBackend

    # Reset module latch so this test controls enablement.
    monkeypatch.setattr(le, "_enabled", False)
    monkeypatch.setattr(le, "_enable_attempted", False)
    monkeypatch.delenv("GRUG_BENCH_LLMOBS", raising=False)
    monkeypatch.delenv("DD_LLMOBS_ENABLED", raising=False)
    monkeypatch.delenv("DD_API_KEY", raising=False)

    assert le.maybe_enable() is False
    trial = TrialResult(
        concurrency=1, fixture="small", backend="cave",
        ttft_s=0.1, complete_s=1.0, parse_ok=True, errored=False,
        prompt_chars=10, response_chars=5, completion_tokens=1,
    )
    backend = BenchBackend(
        name="cave", url="http://x/v1/chat/completions", model="m", api_key="",
    )
    # Must not raise when disabled.
    le.emit_trial(backend, [{"role": "user", "content": "hi"}], trial)
    le.flush()


def test_llmobs_trial_span_submits_span_and_evals(monkeypatch):
    """When enabled, trial_span wraps the call and submits parse_ok + complete_s."""
    from review_latency import llmobs_export as le
    from sast_benchmark.backends import BenchBackend

    annotate_calls: list[dict] = []
    eval_calls: list[dict] = []
    llm_kwargs: list[dict] = []

    class _FakeSpan:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeLLMObs:
        @staticmethod
        def enable(**kwargs):
            return None

        @staticmethod
        def llm(**kwargs):
            llm_kwargs.append(kwargs)
            return _FakeSpan()

        @staticmethod
        def annotate(**kwargs):
            annotate_calls.append(kwargs)

        @staticmethod
        def export_span(span=None):
            return {"span_id": "1", "trace_id": "2"}

        @staticmethod
        def submit_evaluation(**kwargs):
            eval_calls.append(kwargs)

        @staticmethod
        def flush():
            return None

    monkeypatch.setattr(le, "_enabled", False)
    monkeypatch.setattr(le, "_enable_attempted", False)
    monkeypatch.setenv("GRUG_BENCH_LLMOBS", "1")
    monkeypatch.setenv("DD_API_KEY", "test-key")

    fake_mod = type(sys)("ddtrace.llmobs")
    fake_mod.LLMObs = _FakeLLMObs
    monkeypatch.setitem(sys.modules, "ddtrace", type(sys)("ddtrace"))
    monkeypatch.setitem(sys.modules, "ddtrace.llmobs", fake_mod)

    assert le.maybe_enable() is True
    trial = TrialResult(
        concurrency=1, fixture="small", backend="cave",
        ttft_s=0.2, complete_s=3.5, parse_ok=True, errored=False,
        prompt_chars=100, response_chars=50, completion_tokens=12,
    )
    backend = BenchBackend(
        name="cave",
        url="http://x/v1/chat/completions",
        model="north-mini-code-1.0:bf16",
        api_key="",
    )
    with le.trial_span(
        backend,
        [{"role": "user", "content": "review me"}],
    ) as handle:
        handle.finish(trial, output_preview='{"findings":[]}')
    assert llm_kwargs and llm_kwargs[0]["name"] == "elder_latency_bakeoff"
    assert llm_kwargs[0]["model_name"] == "north-mini-code-1.0:bf16"
    assert annotate_calls and annotate_calls[0]["metadata"]["kind"] == "parse_ok"
    labels = {c["label"] for c in eval_calls}
    assert labels >= {"parse_ok", "complete_s", "ttft_s"}
    parse_eval = next(c for c in eval_calls if c["label"] == "parse_ok")
    assert parse_eval["value"] == "true"
    complete_eval = next(c for c in eval_calls if c["label"] == "complete_s")
    assert complete_eval["value"] == 3.5


def _install_fake_llmobs(monkeypatch, le):
    """Enable export against a fake LLMObs; returns (llm_kwargs, exits)."""
    llm_kwargs: list[dict] = []
    exits: list[tuple] = []

    class _FakeSpan:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            exits.append(a)
            return False

    class _FakeLLMObs:
        @staticmethod
        def enable(**kwargs):
            return None

        @staticmethod
        def llm(**kwargs):
            llm_kwargs.append(kwargs)
            return _FakeSpan()

        @staticmethod
        def annotate(**kwargs):
            return None

        @staticmethod
        def export_span(span=None):
            return None

        @staticmethod
        def submit_evaluation(**kwargs):
            return None

        @staticmethod
        def flush():
            return None

    monkeypatch.setattr(le, "_enabled", False)
    monkeypatch.setattr(le, "_enable_attempted", False)
    monkeypatch.setenv("DD_API_KEY", "test-key")
    fake_mod = type(sys)("ddtrace.llmobs")
    fake_mod.LLMObs = _FakeLLMObs
    monkeypatch.setitem(sys.modules, "ddtrace", type(sys)("ddtrace"))
    monkeypatch.setitem(sys.modules, "ddtrace.llmobs", fake_mod)
    return llm_kwargs, exits


def test_llmobs_trial_span_propagates_caller_exception(monkeypatch):
    """An exception in the with-body must PROPAGATE (never be swallowed by
    the o11y guard or masked by contextlib's 'generator didn't stop after
    throw()') and the span must still be closed with the error info."""
    from review_latency import llmobs_export as le
    from sast_benchmark.backends import BenchBackend

    monkeypatch.setenv("GRUG_BENCH_LLMOBS", "1")
    llm_kwargs, exits = _install_fake_llmobs(monkeypatch, le)
    assert le.maybe_enable() is True
    backend = BenchBackend(name="cave", url="http://x", model="m", api_key="")

    with pytest.raises(httpx.DecodingError):
        with le.trial_span(
            backend, [{"role": "user", "content": "x"}],
        ):
            raise httpx.DecodingError("corrupt gzip")
    assert llm_kwargs, "span must have been opened"
    assert exits and exits[0][0] is httpx.DecodingError, (
        "span must close with the caller's exc info"
    )


def test_llmobs_flag_is_case_insensitive(monkeypatch):
    """GRUG_BENCH_LLMOBS=TRUE must enable export like DD_LLMOBS_ENABLED."""
    from review_latency import llmobs_export as le

    monkeypatch.setenv("GRUG_BENCH_LLMOBS", "TRUE")
    monkeypatch.delenv("DD_LLMOBS_ENABLED", raising=False)
    assert le._want_llmobs() is True


def test_run_one_stream_transport_failure_finishes_span_then_retries(monkeypatch):
    """The failed stream attempt's span is finished (errored) INSIDE its own
    trial_span; the blocking retry runs OUTSIDE it - no unfinished outer span,
    no nested double-count."""
    from review_latency import runner as rn

    finished: list = []

    class _Obs:
        def finish(self, trial, *, output_preview=""):
            finished.append((trial, output_preview))

    from contextlib import contextmanager

    span_state = {"open": 0}

    @contextmanager
    def _fake_trial_span(*a, **kw):
        span_state["open"] += 1
        try:
            yield _Obs()
        finally:
            span_state["open"] -= 1

    monkeypatch.setattr(rn, "trial_span", _fake_trial_span)

    def _fake_blocking(backend, fixture, *, concurrency_label, timeout_s):
        assert span_state["open"] == 0, "retry must run outside the stream span"
        return TrialResult(
            concurrency=concurrency_label, fixture=fixture.name,
            backend=backend.name, ttft_s=None, complete_s=1.0,
            parse_ok=True, errored=False, prompt_chars=1,
            response_chars=1, completion_tokens=None,
        )

    monkeypatch.setattr(rn, "run_one_blocking", _fake_blocking)
    monkeypatch.setattr(
        rn.httpx, "stream",
        MagicMock(side_effect=httpx.ConnectError("stream-hostile")),
    )
    from sast_benchmark.backends import BenchBackend

    backend = BenchBackend(name="cave", url="http://x", model="m", api_key="")
    fixture = rn.LatencyFixture(
        name="small", added_lines=1,
        messages=({"role": "user", "content": "x"},),
        prompt_chars=1,
    )
    out = rn.run_one_stream(backend, fixture, concurrency_label=1)
    assert out.parse_ok is True and out.errored is False
    assert len(finished) == 1, "stream attempt span must be finished exactly once"
    assert finished[0][0].errored is True
    assert "stream transport failed" in finished[0][1]
