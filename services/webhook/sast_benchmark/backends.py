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
_OPENROUTER_DEFAULT_MODEL = "anthropic/claude-opus-4.7"
_POOLSIDE_DEFAULT_MODEL = "poolside/laguna-m.1"

_OPENROUTER_EXTRA_BODY = {
    "max_tokens": 32_768,
    "reasoning": {"effort": "high", "exclude": True},
}

# Poolside's laguna-m.1 runs thinking ON by default (blew the read timeout +
# broke JSON parse — see llm_client). Disable it for the benchmark too.
_POOLSIDE_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}

# #544: the Cave's require-keys response schema. Ollama maps a bare
# `{"type": "json_object"}` to `format=json`, which silently TRUNCATES
# multi-item answers (the known estate trap) — the #537 Cave baseline
# (catch 0.125) likely undercounted because of it. A json_schema with
# required keys forces the full findings envelope. The shape mirrors what
# Elder's parser (`llm_client._parse_response` -> `_coerce_finding`)
# requires: {"findings": [{path, line, rule, severity, message}]} with
# severity from review_types.SEVERITIES. Cloud backends KEEP json_object so
# their numbers stay comparable with the #537 baselines.
_CAVE_FINDINGS_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "review_findings",
        "schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "line": {"type": "integer"},
                            "rule": {"type": "string"},
                            "severity": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "critical"],
                            },
                            "message": {"type": "string"},
                        },
                        "required": ["path", "line", "rule", "severity", "message"],
                    },
                },
            },
            "required": ["findings"],
        },
    },
}


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
                extra_body=dict(_OPENROUTER_EXTRA_BODY),
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
                # #544: extra_body lands AFTER the runner's default
                # response_format in the POST body dict, so this replaces the
                # truncation-prone json_object for the Cave only.
                extra_body={"response_format": _CAVE_FINDINGS_RESPONSE_FORMAT},
            )
        )

    return out
