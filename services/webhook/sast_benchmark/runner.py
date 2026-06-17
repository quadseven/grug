"""Live runner for the SAST benchmark (#399, ADR-0006).

Drives the corpus through Elder's REAL review path, one backend at a time, so
the recall/precision numbers are Elder's actual behavior (AC4: no fabricated
verdicts). It reuses `llm_client._build_messages` (Elder's exact prompt) and
`llm_client._parse_response` (Elder's exact parser) — only the TRANSPORT is
the benchmark's own, so it can point at any OpenAI-compatible endpoint
(OpenRouter / Poolside / sparkles-Cave) uniformly.

This module makes network calls — it is NOT imported by the pure-scoring tests
and never runs in the per-PR CI suite. It runs only from the on-demand
`benchmark.sast.yml` job (or a manual `python -m sast_benchmark`).
"""

from __future__ import annotations

import logging

import httpx

# Elder's exact prompt + parser — measuring Elder means using its own prompt,
# not a reimplementation. These are llm_client internals; if their signatures
# change, this runner must follow (it is the whole point of the benchmark).
from llm_client import Hunk, _build_messages, _parse_response

from .backends import BenchBackend
from .corpus import CorpusSample

log = logging.getLogger("grug.sast_benchmark")

_TIMEOUT_SECONDS = 90.0
_RETRY_ATTEMPTS = 2


def _post(backend: BenchBackend, messages: list[dict[str, str]]) -> httpx.Response:
    """One OpenAI-compatible chat-completions POST with a small retry. Raises on
    persistent transport failure — the caller records that backend/sample as
    UNFLAGGED with an error note rather than fabricating a verdict."""
    body = {
        "model": backend.model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        **backend.extra_body,
    }
    headers = {}
    if backend.api_key:
        headers["Authorization"] = f"Bearer {backend.api_key}"
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return httpx.post(
                backend.url, json=body, headers=headers, timeout=_TIMEOUT_SECONDS
            )
        except (httpx.RequestError, httpx.TimeoutException) as e:  # transient
            last_exc = e
            log.warning(
                "bench_backend_post_failed",
                extra={"backend": backend.name, "attempt": attempt, "kind": type(e).__name__},
            )
    assert last_exc is not None
    raise last_exc


def run_sample(backend: BenchBackend, sample: CorpusSample) -> int:
    """Run ONE corpus sample through ONE backend; return the count of findings
    Elder reported ON THIS SAMPLE'S PATH (the scoring "flagged" signal).

    A transport/parse failure returns 0 (unflagged) + logs — it does NOT raise,
    so one flaky sample can't abort the whole sweep, and it is NOT counted as a
    detection (an errored backend reads as "found nothing", the honest floor).
    """
    hunks = [Hunk(path=sample.path, body=sample.diff_body)]
    try:
        messages = _build_messages(hunks, "benchmark")
        resp = _post(backend, messages)
        findings, _model, err = _parse_response(resp)
    except Exception as e:  # noqa: BLE001 — one sample must not abort the sweep
        log.warning(
            "bench_sample_errored",
            extra={"backend": backend.name, "sample": sample.name, "kind": type(e).__name__},
        )
        return 0
    if err:
        log.info(
            "bench_sample_parse_degraded",
            extra={"backend": backend.name, "sample": sample.name, "err": err},
        )
    # Count only findings on THIS sample's path (each corpus path is unique, so
    # cross-sample contamination is impossible, but match defensively).
    return sum(1 for f in findings if f.path == sample.path)


def run_backend(
    backend: BenchBackend, corpus: tuple[CorpusSample, ...]
) -> dict[str, int]:
    """Run the whole corpus through one backend; return sample-name -> finding
    count (the `findings_by_sample` input `scoring.score` expects)."""
    log.info("bench_backend_start", extra={"backend": backend.name, "samples": len(corpus)})
    return {s.name: run_sample(backend, s) for s in corpus}
