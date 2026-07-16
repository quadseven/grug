"""Live concurrency latency runner (#648).

Network-only; never imported by pure unit tests. Reuses Elder's prompt
construction and parse path so timings reflect real review prefill, not
short smokes.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Sequence

import httpx

from llm_client import _parse_response
from sast_benchmark.backends import BenchBackend

from .fixtures import LatencyFixture
from .scoring import TrialResult

log = logging.getLogger("grug.review_latency")

_DEFAULT_TIMEOUT_S = 330.0


def _headers(backend: BenchBackend) -> dict[str, str]:
    if not backend.api_key:
        return {}
    return {"Authorization": f"Bearer {backend.api_key}"}


def _post_body(backend: BenchBackend, messages: Sequence[dict[str, str]], *, stream: bool) -> dict:
    body: dict = {
        "model": backend.model,
        "messages": list(messages),
        "response_format": {"type": "json_object"},
        **backend.extra_body,
    }
    if stream:
        body["stream"] = True
    return body


def run_one_stream(
    backend: BenchBackend,
    fixture: LatencyFixture,
    *,
    concurrency_label: int,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> TrialResult:
    """One request with streaming TTFT when the server supports SSE."""
    t0 = time.perf_counter()
    ttft: float | None = None
    chunks: list[str] = []
    try:
        with httpx.stream(
            "POST",
            backend.url,
            json=_post_body(backend, fixture.messages, stream=True),
            headers=_headers(backend),
            timeout=timeout_s,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                if ttft is None:
                    ttft = time.perf_counter() - t0
                # OpenAI SSE: "data: {...}" or raw JSON lines.
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunks.append(payload)
                else:
                    chunks.append(line)
        complete = time.perf_counter() - t0
        raw = "\n".join(chunks)
        # Non-stream servers may ignore stream=true and return one JSON blob
        # as a single "line" — still parseable via a fake Response.
        fake = httpx.Response(200, text=raw if raw.startswith("{") else _sse_to_content(raw))
        if not fake.text.startswith("{"):
            # Rebuild OpenAI chat completion from accumulated delta content.
            content = _sse_to_content(raw)
            fake = httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": content, "role": "assistant"}}],
                    "model": backend.model,
                },
            )
        findings, _model, err = _parse_response(fake)
        return TrialResult(
            concurrency=concurrency_label,
            fixture=fixture.name,
            backend=backend.name,
            ttft_s=ttft,
            complete_s=complete,
            parse_ok=err is None or err == "",
            errored=False,
            prompt_chars=fixture.prompt_chars,
            response_chars=len(fake.text),
        )
    except Exception as e:  # noqa: BLE001 - one trial must not abort the sweep
        log.warning(
            "latency_trial_stream_failed",
            extra={
                "backend": backend.name,
                "fixture": fixture.name,
                "kind": type(e).__name__,
            },
        )
        # Fall back to non-stream so a stream-hostile backend still yields
        # complete wall-clock numbers.
        return run_one_blocking(
            backend, fixture, concurrency_label=concurrency_label, timeout_s=timeout_s,
        )


def _sse_to_content(raw: str) -> str:
    """Best-effort extract of assistant text from SSE JSON fragments."""
    import json

    parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line == "[DONE]":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        choices = obj.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get("delta") or {}
        if isinstance(delta, dict) and delta.get("content"):
            parts.append(str(delta["content"]))
            continue
        msg = choices[0].get("message") or {}
        if isinstance(msg, dict) and msg.get("content"):
            parts.append(str(msg["content"]))
    return "".join(parts)


def run_one_blocking(
    backend: BenchBackend,
    fixture: LatencyFixture,
    *,
    concurrency_label: int,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> TrialResult:
    """Non-stream POST: complete wall-clock only (TTFT left None)."""
    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            backend.url,
            json=_post_body(backend, fixture.messages, stream=False),
            headers=_headers(backend),
            timeout=timeout_s,
        )
        complete = time.perf_counter() - t0
        resp.raise_for_status()
        findings, _model, err = _parse_response(resp)
        _ = findings
        return TrialResult(
            concurrency=concurrency_label,
            fixture=fixture.name,
            backend=backend.name,
            ttft_s=None,
            complete_s=complete,
            parse_ok=not err,
            errored=False,
            prompt_chars=fixture.prompt_chars,
            response_chars=len(resp.text),
        )
    except Exception as e:  # noqa: BLE001
        complete = time.perf_counter() - t0
        log.warning(
            "latency_trial_failed",
            extra={
                "backend": backend.name,
                "fixture": fixture.name,
                "kind": type(e).__name__,
            },
        )
        return TrialResult(
            concurrency=concurrency_label,
            fixture=fixture.name,
            backend=backend.name,
            ttft_s=None,
            complete_s=complete,
            parse_ok=False,
            errored=True,
            prompt_chars=fixture.prompt_chars,
            response_chars=0,
        )


def run_concurrency_cell(
    backend: BenchBackend,
    fixtures: Sequence[LatencyFixture],
    concurrency: int,
    *,
    stream: bool = True,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> list[TrialResult]:
    """Fire `concurrency` parallel requests cycling through fixtures."""
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    work: list[LatencyFixture] = []
    for i in range(concurrency):
        work.append(fixtures[i % len(fixtures)])

    runner = run_one_stream if stream else run_one_blocking
    results: list[TrialResult] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [
            pool.submit(
                runner,
                backend,
                fx,
                concurrency_label=concurrency,
                timeout_s=timeout_s,
            )
            for fx in work
        ]
        for fut in as_completed(futs):
            results.append(fut.result())
    return results


def sweep(
    backend: BenchBackend,
    fixtures: Sequence[LatencyFixture],
    levels: Sequence[int] = (1, 2, 4, 8),
    *,
    stream: bool = True,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> list[TrialResult]:
    """Run each concurrency level sequentially (levels themselves are serial)."""
    all_trials: list[TrialResult] = []
    for c in levels:
        log.info(
            "latency_cell_start",
            extra={"backend": backend.name, "concurrency": c, "fixtures": len(fixtures)},
        )
        all_trials.extend(
            run_concurrency_cell(
                backend, fixtures, c, stream=stream, timeout_s=timeout_s,
            )
        )
    return all_trials
