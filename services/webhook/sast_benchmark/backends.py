"""Backend configuration for the SAST benchmark live runner (#399, ADR-0006).

Backend-pluggable: OpenRouter, Poolside, AND the Cave (Ollama) are
all first-class backends, each configured ENTIRELY from env so NOTHING
sensitive is committed to this public repo. The Cave endpoint is a
private tailnet address and a key (if any) — supplied at run time via the
benchmark CI job's secrets, never a literal here.

`configured_backends()` returns whichever backends have an endpoint + (where
required) a key present, so a run on a box that can only reach the public
clouds still produces a partial baseline; adding the Cave endpoint secret
enables it with NO code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from openrouter_free_limiter import is_free_tier_model


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
# Same retirement as llm_client._POOLSIDE_MODEL -- `laguna-m.1` 404s
# upstream; the key is fine. See that constant for the measurement.
_POOLSIDE_DEFAULT_MODEL = "poolside/laguna-s-2.1"

_OPENROUTER_EXTRA_BODY = {
    "max_tokens": 32_768,
    "reasoning": {"effort": "high", "exclude": True},
}

# grug#881: a `:free` model override is whatever an operator points
# GRUG_BENCH_OPENROUTER_MODEL at to measure the free tier - unlike the
# default (opus-4.7, chosen to mirror production's exact reasoning config
# so the bench stays comparable), it is not guaranteed to implement
# OpenRouter's unified `reasoning.exclude` the way a first-party frontier
# model does. Verified LIVE against `poolside/laguna-s-2.1:free` with the
# real request `_build_messages` + this module's default extra_body
# produce: `reasoning: {"effort": "high", "exclude": True}` let the model
# spend its ENTIRE completion-token budget on reasoning OpenRouter was
# told to exclude from the response, so `finish_reason="length"` and BOTH
# `message.content` AND `message.reasoning` came back `None` - excluded
# means excluded, there was no `reasoning` field to fall back to, every
# case failed before a single token of usable output was produced.
#
# grug#852 disabled the same class of runaway thinking on the Cave arm via
# `chat_template_kwargs: {"enable_thinking": False}` - that mechanism does
# NOT reach this model through OpenRouter (verified live: byte-identical
# null-content result with or without it added to the request body).
# OpenRouter's OWN `reasoning: {"enabled": False}` toggle DOES turn it off
# (verified live: finish_reason="stop", non-null content). Applied only to
# a `:free` override, never the default, so the bench still measures
# production's actual opus-4.7 reasoning config faithfully.
_FREE_TIER_OPENROUTER_REASONING = {"enabled": False}

# Poolside's laguna-s-2.1 runs thinking ON by default (blew the read timeout +
# broke JSON parse — see llm_client). Production sends this same switch at
# three call sites in llm_client.py to keep it off; #894: the Cave backend
# below needs it too, for the identical reason - a thinking model burns its
# whole completion budget reasoning under this module's constrained
# json_schema decoder and emits an empty findings list, which then scores as
# a clean review that "found nothing" rather than as the truncation it is.
_DISABLE_THINKING_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}
_POOLSIDE_EXTRA_BODY = _DISABLE_THINKING_EXTRA_BODY

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
        # `or`, not getenv's default: benchmark.elder-eval.yml always DEFINES
        # this variable (`GRUG_BENCH_OPENROUTER_MODEL: ${{ inputs.
        # openrouter_model }}`) and that input defaults to "". A defined-but-
        # empty variable is not an absent one, so getenv's default never fired
        # and a blank dispatch built `model=""` - POSTing an empty model id
        # instead of the paid default the workflow's own comment promises
        # ("Blank keeps the configured default"). The sibling Cave backend
        # below guards the same shape with `if cave_url and cave_model`.
        openrouter_model = (
            os.getenv("GRUG_BENCH_OPENROUTER_MODEL", "").strip()
            or _OPENROUTER_DEFAULT_MODEL
        )
        openrouter_extra_body = dict(_OPENROUTER_EXTRA_BODY)
        if is_free_tier_model(openrouter_model):
            # See _FREE_TIER_OPENROUTER_REASONING's docstring (grug#881).
            openrouter_extra_body["reasoning"] = dict(
                _FREE_TIER_OPENROUTER_REASONING
            )
        out.append(
            BenchBackend(
                name="openrouter",
                url=os.getenv(
                    "GRUG_BENCH_OPENROUTER_URL",
                    "https://openrouter.ai/api/v1/chat/completions",
                ),
                model=openrouter_model,
                api_key=openrouter_key,
                extra_body=openrouter_extra_body,
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

    # the Cave (Ollama, OpenAI-compatible). URL is a private tailnet
    # address supplied via secret at run time — there is no default and no
    # literal. Key optional (Ollama typically needs none).
    cave_url = os.getenv("GRUG_BENCH_CAVE_URL", "")
    cave_model = os.getenv("GRUG_BENCH_CAVE_MODEL", "")
    if cave_url and cave_model:
        out.append(
            BenchBackend(
                name="cave",
                url=cave_url,
                model=cave_model,
                api_key=os.getenv("GRUG_BENCH_CAVE_KEY", ""),
                # #544: extra_body lands AFTER the runner's default
                # response_format in the POST body dict, so this replaces the
                # truncation-prone json_object for the Cave only. #894: also
                # carries the same thinking-off switch production sends -
                # without it, a thinking model spends its whole budget
                # reasoning under this schema and comes back {"findings": []},
                # which scores as a clean pass instead of the failure it is.
                extra_body={
                    "response_format": _CAVE_FINDINGS_RESPONSE_FORMAT,
                    **_DISABLE_THINKING_EXTRA_BODY,
                },
            )
        )

    return out
