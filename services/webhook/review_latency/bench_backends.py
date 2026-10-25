"""Backend configuration for the review_latency harness (#648).

Minimal copy of the BenchBackend dataclass from the former sast_benchmark.backends,
so the latency harness does not depend on the moved benchmark suites.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class BenchBackend:
    """One OpenAI-compatible chat-completions endpoint to benchmark.

    `name` is the baseline key. `url`/`model`/`api_key` come from env. `api_key`
    may be empty (a self-hosted Ollama often needs none) — emptiness means "no
    Authorization header", NOT "skip this backend". `extra_body` carries
    vendor-specific params. `headers` carries extra request headers.
    """

    name: str
    url: str
    model: str
    api_key: str
    extra_body: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)