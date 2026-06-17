"""Backend configuration for the SAST benchmark live runner (#399, ADR-0006).

Backend-pluggable: OpenRouter, Poolside, AND sparkles/the-Cave (Ollama) are
all first-class backends, each configured ENTIRELY from env so NOTHING
sensitive is committed to this public repo. The Cave/sparkles endpoint is a
private tailnet address and a key (if any) — supplied at run time via the
benchmark CI job's secrets, never a literal here.

`configured_backends()` returns whichever backends have an endpoint + (where
required) a key present, so a run on a box that can only reach the public
clouds still produces a partial baseline; adding the sparkles endpoint secret
enables it with NO code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class BenchBackend:
    """One OpenAI-compatible chat-completions endpoint to benchmark.

    `name` is the baseline key. `url`/`model`/`api_key` come from env. `api_key`
    may be empty (a self-hosted Ollama often needs none) — emptiness means "no
    Authorization header", NOT "skip this backend". `extra_body` carries
    vendor-specific params (e.g. Poolside's thinking-off switch), mirroring
    llm_client's per-backend `extra_body`."""

    name: str
    url: str
    model: str
    api_key: str
    extra_body: dict = field(default_factory=dict)


# Default models mirror llm_client's choices so the benchmark measures the same
# models Elder uses; overridable by env for an apples-to-apples re-run on a new
# model. NO endpoint URL is defaulted for the Cave (it is private/tailnet) — it
# only runs when GRUG_BENCH_CAVE_URL is supplied.
_OPENROUTER_DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
_POOLSIDE_DEFAULT_MODEL = "poolside/laguna-m.1"

# Poolside's laguna-m.1 runs thinking ON by default (blew the read timeout +
# broke JSON parse — see llm_client). Disable it for the benchmark too.
_POOLSIDE_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}


def configured_backends() -> list[BenchBackend]:
    """Build the backend list from env. A cloud backend is included when its
    key env is set; the Cave is included when its URL env is set (key optional).
    Returns only configured backends so a partial environment yields a partial
    (honest) baseline rather than a crash."""
    out: list[BenchBackend] = []

    openrouter_key = os.getenv("GRUG_BENCH_OPENROUTER_KEY", "")
    if openrouter_key:
        out.append(
            BenchBackend(
                name="openrouter",
                url=os.getenv(
                    "GRUG_BENCH_OPENROUTER_URL",
                    "https://openrouter.ai/api/v1/chat/completions",
                ),
                model=os.getenv("GRUG_BENCH_OPENROUTER_MODEL", _OPENROUTER_DEFAULT_MODEL),
                api_key=openrouter_key,
            )
        )

    poolside_key = os.getenv("GRUG_BENCH_POOLSIDE_KEY", "")
    if poolside_key:
        out.append(
            BenchBackend(
                name="poolside",
                url=os.getenv(
                    "GRUG_BENCH_POOLSIDE_URL",
                    "https://inference.poolside.ai/v1/chat/completions",
                ),
                model=os.getenv("GRUG_BENCH_POOLSIDE_MODEL", _POOLSIDE_DEFAULT_MODEL),
                api_key=poolside_key,
                extra_body=dict(_POOLSIDE_EXTRA_BODY),
            )
        )

    # sparkles / the Cave (Ollama, OpenAI-compatible). URL is a private tailnet
    # address supplied via secret at run time — there is no default and no
    # literal. Key optional (Ollama typically needs none).
    cave_url = os.getenv("GRUG_BENCH_CAVE_URL", "")
    cave_model = os.getenv("GRUG_BENCH_CAVE_MODEL", "")
    if cave_url and cave_model:
        out.append(
            BenchBackend(
                name="sparkles",
                url=cave_url,
                model=cave_model,
                api_key=os.getenv("GRUG_BENCH_CAVE_KEY", ""),
            )
        )

    return out
