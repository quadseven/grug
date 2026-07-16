"""Live concurrency latency runner (#648).

Network-only; never imported by pure unit tests. Reuses Elder's prompt
construction and parse path (deliberate private imports, same as
elder_eval/sast_benchmark) so timings reflect real review prefill.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Sequence

import httpx

from llm_client import _parse_response  # noqa: PLC2701 - Elder path on purpose
from sast_benchmark.backends import BenchBackend

from .fixtures import LatencyFixture
from .scoring import TrialResult

log = logging.getLogger("grug.review_latency")

_DEFAULT_TIMEOUT_S = 330.0


def _headers(backend: BenchBackend) -> dict[str, str]:
    if not backend.api_key:
        return {}
    return {"Authorization": f"Bearer {backend.api_key}"}


def _post_body(
    backend: BenchBackend, messages: Sequence[dict[str, str]], *, stream: bool,
) -> dict:
    body: dict = {
        "model": backend.model,
        "messages": list(messages),
        "response_format": {"type": "json_object"},
        **backend.extra_body,
    }
    if stream:
        body["stream"] = True
        # Ask for usage on the final SSE chunk when the server supports it.
        body["stream_options"] = {"include_usage": True}
    return body


def _parse_completion_tokens(raw_sse_or_json: str) -> int | None:
    """Best-effort completion_tokens from a full JSON body or last SSE usage."""
    # Whole-body JSON (non-stream or stream ignored).
    try:
        obj = json.loads(raw_sse_or_json)
        if isinstance(obj, dict):
            usage = obj.get("usage") or {}
            if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
                return int(usage["completion_tokens"])
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    tokens: int | None = None
    for line in raw_sse_or_json.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage") or {}
        if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
            try:
                tokens = int(usage["completion_tokens"])
            except (TypeError, ValueError):
                continue
    return tokens


def _response_from_stream_raw(raw: str, model: str) -> httpx.Response:
    """Build an OpenAI-shaped Response for Elder's parser from stream bytes."""
    if raw.lstrip().startswith("{"):
        return httpx.Response(200, text=raw)
    content = _sse_to_content(raw)
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content, "role": "assistant"}}],
            "model": model,
        },
    )


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
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunks.append(payload)
                else:
                    chunks.append(line)
        complete = time.perf_counter() - t0
        raw = "\n".join(chunks)
        try:
            fake = _response_from_stream_raw(raw, backend.model)
            findings, _model, err = _parse_response(fake)
            _ = findings
            tokens = _parse_completion_tokens(raw)
            return TrialResult(
                concurrency=concurrency_label,
                fixture=fixture.name,
                backend=backend.name,
                ttft_s=ttft,
                complete_s=complete,
                parse_ok=not err,
                errored=False,
                prompt_chars=fixture.prompt_chars,
                response_chars=len(fake.text),
                completion_tokens=tokens,
            )
        except Exception as e:  # noqa: BLE001 - local parse/rebuild, no second POST
            log.warning(
                "latency_trial_stream_parse_failed",
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
                ttft_s=ttft,
                complete_s=complete,
                parse_ok=False,
                errored=True,
                prompt_chars=fixture.prompt_chars,
                response_chars=len(raw),
                completion_tokens=None,
            )
    except (httpx.TransportError, httpx.HTTPStatusError, httpx.TimeoutException) as e:
        log.warning(
            "latency_trial_stream_transport_failed",
            extra={
                "backend": backend.name,
                "fixture": fixture.name,
                "kind": type(e).__name__,
            },
        )
        # Stream-hostile or flaky transport: one non-stream retry.
        return run_one_blocking(
            backend, fixture, concurrency_label=concurrency_label, timeout_s=timeout_s,
        )


def _sse_to_content(raw: str) -> str:
    """Best-effort extract of assistant text from SSE JSON fragments."""
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
        tokens = _parse_completion_tokens(resp.text)
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
            completion_tokens=tokens,
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
            completion_tokens=None,
        )


def run_concurrency_cell(
    backend: BenchBackend,
    fixtures: Sequence[LatencyFixture],
    concurrency: int,
    *,
    stream: bool = True,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> tuple[list[TrialResult], float]:
    """Fire `concurrency` parallel requests; return trials + cell wall-clock."""
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    work: list[LatencyFixture] = []
    for i in range(concurrency):
        work.append(fixtures[i % len(fixtures)])

    runner = run_one_stream if stream else run_one_blocking
    results: list[TrialResult] = []
    t0 = time.perf_counter()
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
    wall = time.perf_counter() - t0
    return results, wall


def sweep(
    backend: BenchBackend,
    fixtures: Sequence[LatencyFixture],
    levels: Sequence[int] = (1, 2, 4, 8),
    *,
    stream: bool = True,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> tuple[list[TrialResult], dict[tuple[str, int], float]]:
    """Run each concurrency level; return trials + cell walls keyed by backend,C."""
    all_trials: list[TrialResult] = []
    walls: dict[tuple[str, int], float] = {}
    for c in levels:
        log.info(
            "latency_cell_start",
            extra={"backend": backend.name, "concurrency": c, "fixtures": len(fixtures)},
        )
        trials, wall = run_concurrency_cell(
            backend, fixtures, c, stream=stream, timeout_s=timeout_s,
        )
        all_trials.extend(trials)
        walls[(backend.name, c)] = wall
    return all_trials, walls
