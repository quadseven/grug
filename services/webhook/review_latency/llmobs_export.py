"""Optional Datadog LLMObs export for live review_latency bakeoffs (#648).

Production Elder already emits `elder_code_review` / `elder_judge` spans.
This harness is the offline/concurrency path - without explicit export,
model bakeoffs (north-mini, Nemotron, ...) never appear in
https://app.datadoghq.com/llm/evaluations .

Enable when:
  - DD_LLMOBS_ENABLED=true  OR  GRUG_BENCH_LLMOBS=1
  - DD_API_KEY is set (agentless path; no local DD agent required)

Default ml_app is `grug-elder-bakeoff` so bakeoff noise stays out of the
production `grug-elder` dashboard. Override with DD_LLMOBS_ML_APP.

Each trial opens one LLM span **around** the HTTP call so span duration
matches wall-clock, then submits score/categorical evaluations:
  - parse_ok (categorical true/false)
  - complete_s (score, seconds)
  - ttft_s (score, when measured)
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence

from sast_benchmark.backends import BenchBackend

from .scoring import TrialResult

log = logging.getLogger("grug.review_latency.llmobs")

_LLMOBS_NAME = "elder_latency_bakeoff"
_DEFAULT_ML_APP = "grug-elder-bakeoff"

_enabled = False
_enable_attempted = False


def _want_llmobs() -> bool:
    if os.getenv("GRUG_BENCH_LLMOBS", "").strip() in ("1", "true", "yes", "on"):
        return True
    return os.getenv("DD_LLMOBS_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def maybe_enable() -> bool:
    """Enable agentless LLMObs once per process. Returns True if export is live."""
    global _enabled, _enable_attempted
    if _enable_attempted:
        return _enabled
    _enable_attempted = True
    if not _want_llmobs():
        log.info("review_latency_llmobs_disabled")
        return False
    api_key = (os.getenv("DD_API_KEY") or "").strip()
    if not api_key:
        log.warning(
            "review_latency_llmobs_missing_api_key",
            extra={"hint": "set DD_API_KEY (SSM /grug/dd-api-key) for agentless export"},
        )
        return False
    ml_app = (os.getenv("DD_LLMOBS_ML_APP") or _DEFAULT_ML_APP).strip() or _DEFAULT_ML_APP
    site = (os.getenv("DD_SITE") or "datadoghq.com").strip() or "datadoghq.com"
    try:
        from ddtrace.llmobs import LLMObs
    except ImportError:
        log.warning("review_latency_llmobs_ddtrace_missing")
        return False
    try:
        LLMObs.enable(
            ml_app=ml_app,
            agentless_enabled=True,
            site=site,
            api_key=api_key,
        )
    except Exception as e:  # noqa: BLE001 - bakeoff must not die on o11y
        log.warning(
            "review_latency_llmobs_enable_failed",
            extra={"kind": type(e).__name__, "detail": str(e)[:200]},
        )
        return False
    _enabled = True
    log.info(
        "review_latency_llmobs_enabled",
        extra={"ml_app": ml_app, "site": site},
    )
    return True


def flush() -> None:
    """Best-effort flush so short CLI runs do not drop the last spans."""
    if not _enabled:
        return
    try:
        from ddtrace.llmobs import LLMObs

        LLMObs.flush()
    except Exception as e:  # noqa: BLE001
        log.warning(
            "review_latency_llmobs_flush_failed",
            extra={"kind": type(e).__name__},
        )


class _TrialSpan:
    """Handle returned by `trial_span` to finish annotate + evaluations."""

    def __init__(
        self,
        *,
        llmobs: Any,
        span: Any,
        backend: BenchBackend,
        fixture_messages: Sequence[dict[str, str]],
        concurrency: int,
        fixture_name: str,
    ) -> None:
        self._llmobs = llmobs
        self._span = span
        self._backend = backend
        self._messages = fixture_messages
        self._concurrency = concurrency
        self._fixture_name = fixture_name

    def finish(self, trial: TrialResult, *, output_preview: str = "") -> None:
        tags = {
            "backend": self._backend.name,
            "model": self._backend.model,
            "fixture": trial.fixture,
            "concurrency": str(trial.concurrency),
            "harness": "review_latency",
        }
        kind = (
            "errored" if trial.errored
            else ("parse_ok" if trial.parse_ok else "parse_failed")
        )
        metrics: dict[str, float | int] = {
            "latency_ms": int(trial.complete_s * 1000),
            "complete_s": float(trial.complete_s),
            "prompt_chars": trial.prompt_chars,
            "response_chars": trial.response_chars,
        }
        if trial.ttft_s is not None:
            metrics["ttft_s"] = float(trial.ttft_s)
            metrics["ttft_ms"] = int(trial.ttft_s * 1000)
        if trial.completion_tokens is not None:
            metrics["output_tokens"] = int(trial.completion_tokens)

        try:
            self._llmobs.annotate(
                span=self._span,
                input_data=list(self._messages),
                output_data=output_preview or None,
                metadata={
                    "backend": self._backend.name,
                    "fixture": trial.fixture,
                    "concurrency": trial.concurrency,
                    "kind": kind,
                    "parse_ok": trial.parse_ok,
                    "errored": trial.errored,
                },
                metrics=metrics,
                tags=tags,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "review_latency_llmobs_annotate_failed",
                extra={"kind": type(e).__name__, "backend": self._backend.name},
            )
            return

        span_context: Optional[dict[str, Any]] = None
        try:
            span_context = self._llmobs.export_span(span=self._span)
        except Exception:  # noqa: BLE001
            span_context = None
        if not span_context:
            return

        try:
            self._llmobs.submit_evaluation(
                span=span_context,
                label="parse_ok",
                metric_type="categorical",
                value="true" if trial.parse_ok and not trial.errored else "false",
                tags=tags,
            )
            self._llmobs.submit_evaluation(
                span=span_context,
                label="complete_s",
                metric_type="score",
                value=float(trial.complete_s),
                tags=tags,
            )
            if trial.ttft_s is not None:
                self._llmobs.submit_evaluation(
                    span=span_context,
                    label="ttft_s",
                    metric_type="score",
                    value=float(trial.ttft_s),
                    tags=tags,
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "review_latency_llmobs_eval_failed",
                extra={"kind": type(e).__name__, "backend": self._backend.name},
            )


class _NoopTrialSpan:
    def finish(self, trial: TrialResult, *, output_preview: str = "") -> None:
        return None


@contextmanager
def trial_span(
    backend: BenchBackend,
    fixture_messages: Sequence[dict[str, str]],
    *,
    concurrency: int,
    fixture_name: str,
) -> Iterator[_TrialSpan | _NoopTrialSpan]:
    """Open an LLMObs span around the HTTP trial so duration == wall-clock."""
    if not _enabled and not maybe_enable():
        yield _NoopTrialSpan()
        return
    try:
        from ddtrace.llmobs import LLMObs
    except ImportError:
        yield _NoopTrialSpan()
        return

    try:
        with LLMObs.llm(
            model_name=backend.model,
            model_provider=backend.name,
            name=_LLMOBS_NAME,
        ) as span:
            yield _TrialSpan(
                llmobs=LLMObs,
                span=span,
                backend=backend,
                fixture_messages=fixture_messages,
                concurrency=concurrency,
                fixture_name=fixture_name,
            )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "review_latency_llmobs_span_failed",
            extra={"kind": type(e).__name__, "backend": backend.name},
        )
        yield _NoopTrialSpan()


def emit_trial(
    backend: BenchBackend,
    fixture_messages: Sequence[dict[str, str]],
    trial: TrialResult,
    *,
    output_preview: str = "",
) -> None:
    """Backward-compat: post-hoc span (duration will be ~0). Prefer trial_span."""
    with trial_span(
        backend,
        fixture_messages,
        concurrency=trial.concurrency,
        fixture_name=trial.fixture,
    ) as handle:
        handle.finish(trial, output_preview=output_preview)
