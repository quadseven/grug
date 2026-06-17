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
from dataclasses import dataclass

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


def run_sample(backend: BenchBackend, sample: CorpusSample) -> tuple[int, bool]:
    """Run ONE corpus sample through ONE backend. Returns `(finding_count,
    errored)`: the count of findings Elder reported ON THIS SAMPLE'S PATH (the
    scoring "flagged" signal), and whether the call ERRORED.

    A transport/parse failure returns `(0, True)` + logs — it does NOT raise,
    so one flaky sample can't abort the sweep. The `errored` flag is
    load-bearing: a count of 0 from a SUCCESSFUL call ("Elder found nothing")
    must be distinguishable from 0 from a FAILED call ("the benchmark could not
    run"), else an all-errored backend would record a bogus zero-recall
    baseline that looks valid.
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
        return (0, True)
    if err:
        log.info(
            "bench_sample_parse_degraded",
            extra={"backend": backend.name, "sample": sample.name, "err": err},
        )
    # Count only findings on THIS sample's path (each corpus path is unique, so
    # cross-sample contamination is impossible, but match defensively).
    return (sum(1 for f in findings if f.path == sample.path), False)


@dataclass(frozen=True, slots=True)
class BackendRun:
    """One backend's sweep over the corpus. `findings_by_sample` feeds
    `scoring.score`; `errors`/`total` let the caller reject a baseline from a
    backend that could not actually run (e.g. all calls errored = bad key/URL),
    rather than recording its bogus all-zero recall as valid."""

    findings_by_sample: dict[str, int]
    errors: int
    total: int

    @property
    def all_errored(self) -> bool:
        """True when EVERY sample errored — the run is broken, not a result."""
        return self.total > 0 and self.errors == self.total


def run_backend(
    backend: BenchBackend, corpus: tuple[CorpusSample, ...]
) -> BackendRun:
    """Run the whole corpus through one backend. Returns a `BackendRun` whose
    `findings_by_sample` is the input `scoring.score` expects, plus the error
    tally so the caller can reject a fully-broken run."""
    log.info("bench_backend_start", extra={"backend": backend.name, "samples": len(corpus)})
    counts: dict[str, int] = {}
    errors = 0
    for s in corpus:
        count, errored = run_sample(backend, s)
        counts[s.name] = count
        if errored:
            errors += 1
    if errors:
        log.warning(
            "bench_backend_errors",
            extra={"backend": backend.name, "errors": errors, "total": len(corpus)},
        )
    return BackendRun(findings_by_sample=counts, errors=errors, total=len(corpus))
