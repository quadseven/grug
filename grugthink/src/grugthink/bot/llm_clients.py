"""LLM API client functions for GrugThink bot.

This module handles communication with different LLM backends:
- Ollama API (local/self-hosted models) - the ALWAYS-tried-first primary,
  via the owned in-cluster spark-gateway.
- Poolside / OpenRouter (query_poolside_api / query_openrouter_api) - a
  bounded, single-shot, short-timeout fallback chain engaged ONLY when
  Ollama/Cave produces no usable reply. Same last-resort-overload-valve
  pattern grug's Elder review persona uses (services/_shared/llm_client.py
  `_saas_overload_fallback_config`), sized for a realtime Discord reply
  instead of a multi-minute review pass - see the timeout comments on each
  function for the full worst-case-time math.
- Google Gemini API (cloud-based models) - final bonus fallback tier, gated
  on GEMINI_API_KEY being configured (query_model in prompts.py wires the
  chain; this module just provides the per-backend calls).
"""

import json
import os
import time
from typing import Any

import requests

from .. import config_legacy as config
from ..logging_config import get_logger

log = get_logger(__name__)

# Shared requests session for connection pooling
session = requests.Session()

# DD LLM Observability seam, same lazy-import/no-op-fallback shape as grug's
# own services/_shared/llm_client.py: local dev/tests (no ddtrace installed,
# or DD_LLMOBS_ENABLED unset) get a no-op span instead of an ImportError.
# Wrapped behind module-level indirection so tests can monkeypatch
# `_llmobs_llm` / `_llmobs_annotate` without touching the real SDK. Prior to
# this, grugthink had NO LLM Obs instrumentation at all (2026-07-13 audit) -
# grug-elder's review pipeline was already emitting real ml_app:grug-elder
# spans, but Discord chat was invisible in the DD LLM Obs UI because there
# was genuinely nothing shipping, not because of a filter/view issue.
try:  # pragma: no cover — import-time guard
    from ddtrace.llmobs import LLMObs as _LLMObs

    def _llmobs_llm(**kwargs: Any) -> Any:
        return _LLMObs.llm(**kwargs)

    def _llmobs_annotate(**kwargs: Any) -> None:
        _LLMObs.annotate(**kwargs)
except ImportError:  # pragma: no cover — local dev without ddtrace

    class _NoopSpan:
        def __enter__(self) -> "_NoopSpan":
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

    def _llmobs_llm(**kwargs: Any) -> Any:
        return _NoopSpan()

    def _llmobs_annotate(**kwargs: Any) -> None:
        return None


_LLMOBS_NAME = "grugthink_chat_reply"


def _elapsed_ms(start_ns: int) -> int:
    """`time.monotonic_ns` avoids clock-skew during the span."""
    return (time.monotonic_ns() - start_ns) // 1_000_000


# Primary (Ollama/spark-gateway) budget: one wall-clock deadline for the
# whole primary phase, across every OLLAMA_URLS entry.
#
# Defaults to 60s, overridable with GRUGTHINK_OLLAMA_TIMEOUT_S so a heavier
# resident model can be given more room without a code change. 60s is
# sized from measurements, not guessed (#1045, 2026-09-23, the persona
# prompt at num_predict=150 through the gateway's spark:warm-any): warm
# reply 0.85s end to end, cold model load plus reply 5.4s, so more than 10x
# headroom over a cold load. The 2026-07 incident that raised this came
# from a 122B model under two concurrent long coding turns; the resident
# chat model is now a 30B MoE and chat is tagged realtime at the gateway.
#
# Why a deadline and not just a requests read timeout: a read timeout is
# socket INACTIVITY, and the gateway sends a filler byte every ~15s while a
# request is queued, which resets it every time. `_read_json_before` checks
# the deadline between chunks, and each attempt's socket timeouts are the
# budget remaining when it is sent, so the primary phase ends within 2x the
# budget in the worst case (a read that starts just before the deadline).
#
# A client timeout does not cancel the generation on the server; it only
# orphans it. The bounded fallback chain below still answers when the
# primary truly stalls, so the budget only has to cover real latency.
_OLLAMA_CONNECT_TIMEOUT_S = 10
_OLLAMA_BUDGET_DEFAULT_S = 60
# Upper bound on the override. At the cap the primary phase ends within
# 2 x 400 = 800s, and every fallback tier (15 + 15 + 30s) still fits inside
# Discord's 900s interaction-followup window.
_OLLAMA_BUDGET_MAX_S = 400
_OLLAMA_BUDGET_ENV = "GRUGTHINK_OLLAMA_TIMEOUT_S"
_READ_CHUNK_BYTES = 8192


def _ollama_budget_s() -> int:
    """Wall-clock seconds for the primary phase. An unset, unparseable, or
    out-of-range override falls back to the default, never to no limit."""
    raw = os.getenv(_OLLAMA_BUDGET_ENV, "").strip()
    if not raw:
        return _OLLAMA_BUDGET_DEFAULT_S
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if 1 <= value <= _OLLAMA_BUDGET_MAX_S:
        return value
    log.warning(
        "Ignoring invalid Ollama timeout override",
        extra={"env": _OLLAMA_BUDGET_ENV, "value": raw, "default_s": _OLLAMA_BUDGET_DEFAULT_S},
    )
    return _OLLAMA_BUDGET_DEFAULT_S


def _read_json_before(r: requests.Response, deadline: float) -> Any:
    """Read a streamed JSON body, giving up once `deadline` (monotonic) has
    passed even if filler bytes keep arriving."""
    chunks = []
    for chunk in r.iter_content(chunk_size=_READ_CHUNK_BYTES):
        chunks.append(chunk)
        if time.monotonic() > deadline:
            raise requests.exceptions.ReadTimeout("Ollama primary deadline passed while reading the response")
    body = b"".join(chunks)
    try:
        return json.loads(body)
    except ValueError as e:
        # A truncated or non-JSON 200 body (a proxy error page, a gateway that
        # died after the headers) is a transport failure: raise it as one so
        # the RequestException handler names it and the fallback engages.
        raise requests.exceptions.InvalidJSONError(
            f"Ollama 200 response body is not JSON ({len(body)} bytes): {e}"
        ) from e


# X-Spark-Priority: this URL is the in-cluster spark-gateway (OLLAMA_URLS is
# set to it in k8s/deployment.yaml, not to the Sparks directly) - a Discord
# reply is latency-sensitive and must never queue behind one of Hermes's
# long agentic turns on the shared Ollama target. "realtime" (not
# "interactive"): live incident 2026-07-13 - this call queued behind Grug's
# OWN code-review calls (both tagged "interactive", FIFO within the tier put
# chat second) for 24+ minutes with no client-side timeout ever firing (the
# gateway's queue-wait heartbeat kept resetting it). A stalled Discord reply
# reads as "the bot is broken" within seconds, so it needs to win over
# Grug's own async review work, not just over Hermes's batch turns.
# X-Spark-Caller identifies this consumer in the gateway's own
# metrics/dashboard instead of falling back to a generic "python (ip)" UA
# guess. Harmless if OLLAMA_URLS ever points straight at a Spark instead -
# Ollama ignores unknown headers.
_OLLAMA_HEADERS = {"X-Spark-Priority": "realtime", "X-Spark-Caller": "grugthink-chat"}


def _ollama_request(url: str, model: str, prompt_text: str, openai_compatible: bool) -> tuple[str, dict[str, Any]]:
    """(endpoint, payload) for one primary attempt."""
    if openai_compatible:
        return f"{url}/v1/chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": prompt_text}],
            "stream": False,
            "temperature": 0.5,
            "top_p": 0.7,
            "max_tokens": 150,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    return f"{url}/api/generate", {
        "model": model,
        "prompt": prompt_text,
        "stream": False,
        # Disable the model's reasoning mode. Qwen3 (and other thinking
        # models on the gateway) otherwise spend the WHOLE num_predict
        # budget on internal <think> tokens, returning an empty `response`
        # (done_reason=length) - which the caller reads as None and the
        # bot posts nothing. Verified live: think=false -> real reply.
        "think": False,
        # 150 (was 80): richer replies now that reasoning tokens no longer
        # eat the budget. temperature 0.5 for a little more personality.
        "options": {"num_predict": 150, "temperature": 0.5, "top_p": 0.7, "stop": ["<END>"]},
    }


def _post_before(endpoint: str, payload: dict[str, Any], remaining_s: float, deadline: float) -> tuple[int, Any]:
    """One primary POST inside the remaining budget: (status_code, parsed
    body or None for a non-200). The connection is always released."""
    r = session.post(
        endpoint,
        json=payload,
        headers=_OLLAMA_HEADERS,
        timeout=(min(_OLLAMA_CONNECT_TIMEOUT_S, remaining_s), remaining_s),
        stream=True,
    )
    try:
        body = _read_json_before(r, deadline) if r.status_code == 200 else None
    finally:
        r.close()
    return r.status_code, body


def _ollama_reply_text(body: Any, openai_compatible: bool) -> str:
    if openai_compatible:
        return body["choices"][0]["message"]["content"].strip()
    return body.get("response", "").strip()


def query_ollama_api(
    prompt_text: str, cache_key: str, server_db=None, personality_name: str = None, bot_id: str = None
) -> str | None:
    """Query Ollama API for LLM response.

    Args:
        prompt_text: The prompt to send to the model
        cache_key: Cache key for tracking this request
        server_db: Server database instance for storing lore
        personality_name: Name of the bot personality
        bot_id: Unique identifier for this bot instance

    Returns:
        Validated response string or None if all attempts failed
    """
    # Import here to avoid circular dependency
    from .prompts import validate_and_process_response

    log.info(
        "Starting Ollama API query",
        extra={
            "bot_id": bot_id,
            "personality": personality_name,
            "prompt_length": len(prompt_text),
            "cache_key": cache_key,
            "ollama_urls_count": len(config.OLLAMA_URLS),
            "ollama_urls": config.OLLAMA_URLS,
        },
    )

    # CRITICAL: Check if OLLAMA_URLS is empty - this indicates a configuration error
    if not config.OLLAMA_URLS:
        log.error(
            "OLLAMA_URLS is empty - cannot query Ollama API",
            extra={
                "bot_id": bot_id,
                "personality": personality_name,
                "cache_key": cache_key,
                "ollama_urls": config.OLLAMA_URLS,
                "error": "OLLAMA_URLS environment variable is not set or is empty",
            },
        )
        return None

    budget_s = _ollama_budget_s()
    deadline = time.monotonic() + budget_s
    for idx, url in enumerate(config.OLLAMA_URLS):
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            log.error(
                "Ollama primary budget exhausted before trying every URL",
                extra={"bot_id": bot_id, "budget_s": budget_s, "urls_tried": idx, "cache_key": cache_key},
            )
            break
        raw_model = config.OLLAMA_MODELS[idx] if idx < len(config.OLLAMA_MODELS) else config.OLLAMA_MODELS[0]
        openai_compatible = os.getenv("GRUGTHINK_LLM_API", "ollama").lower() == "openai"
        span_tags = {"bot_id": str(bot_id or ""), "personality": str(personality_name or "")}
        start_ns = time.monotonic_ns()
        with _llmobs_llm(model_name=raw_model, model_provider="ollama", name=_LLMOBS_NAME) as span:
            try:
                endpoint, payload = _ollama_request(url, raw_model, prompt_text, openai_compatible)
                status_code, response_body = _post_before(endpoint, payload, remaining_s, deadline)
                if status_code == 200:
                    response = _ollama_reply_text(response_body, openai_compatible)
                    log.info(
                        "Ollama API response received",
                        extra={
                            "bot_id": bot_id,
                            "personality": personality_name,
                            "model": raw_model,
                            "url": url,
                            "response_length": len(response),
                            "cache_key": cache_key,
                        },
                    )
                    _llmobs_annotate(
                        span=span,
                        input_data=prompt_text,
                        output_data=response,
                        metadata={"model": raw_model, "url": url, "status_code": status_code},
                        metrics={"latency_ms": _elapsed_ms(start_ns)},
                        tags=span_tags,
                    )
                    validated = validate_and_process_response(response, cache_key, server_db, personality_name, bot_id)
                    if validated:
                        return validated
                else:
                    log.warning(
                        "Ollama API returned error",
                        extra={"bot_id": bot_id, "url": url, "status_code": status_code, "model": raw_model},
                    )
                    _llmobs_annotate(
                        span=span,
                        input_data=prompt_text,
                        metadata={"model": raw_model, "url": url, "error": f"http_{status_code}"},
                        metrics={"latency_ms": _elapsed_ms(start_ns)},
                        tags=span_tags,
                    )
            except requests.exceptions.Timeout as e:
                log.error(
                    "Ollama request timed out",
                    extra={
                        "bot_id": bot_id,
                        "url": url,
                        "model": raw_model,
                        "error": str(e),
                        "budget_s": budget_s,
                        "attempt_timeout_s": round(remaining_s, 1),
                    },
                )
                _llmobs_annotate(
                    span=span,
                    input_data=prompt_text,
                    metadata={"model": raw_model, "url": url, "error": "Timeout"},
                    metrics={"latency_ms": _elapsed_ms(start_ns)},
                    tags=span_tags,
                )
            except requests.exceptions.ConnectionError as e:
                log.error(
                    "Ollama connection failed",
                    extra={"bot_id": bot_id, "url": url, "model": raw_model, "error": str(e)},
                )
                _llmobs_annotate(
                    span=span,
                    input_data=prompt_text,
                    metadata={"model": raw_model, "url": url, "error": "ConnectionError"},
                    metrics={"latency_ms": _elapsed_ms(start_ns)},
                    tags=span_tags,
                )
            except requests.exceptions.RequestException as e:
                log.error(
                    "Ollama request failed",
                    extra={
                        "bot_id": bot_id,
                        "url": url,
                        "model": raw_model,
                        "error": str(e),
                        "error_type": type(e).__name__,
                    },
                )
                _llmobs_annotate(
                    span=span,
                    input_data=prompt_text,
                    metadata={"model": raw_model, "url": url, "error": type(e).__name__},
                    metrics={"latency_ms": _elapsed_ms(start_ns)},
                    tags=span_tags,
                )
            except Exception as e:
                log.error(
                    "Unexpected error in Ollama request",
                    extra={
                        "bot_id": bot_id,
                        "url": url,
                        "model": raw_model,
                        "error": str(e),
                        "error_type": type(e).__name__,
                    },
                )
                _llmobs_annotate(
                    span=span,
                    input_data=prompt_text,
                    metadata={"model": raw_model, "url": url, "error": type(e).__name__},
                    metrics={"latency_ms": _elapsed_ms(start_ns)},
                    tags=span_tags,
                )
    return None


# --- Bounded SaaS fallback chain (Poolside, then OpenRouter) -------------
#
# Engaged ONLY when query_ollama_api above returns None (Cave/spark-gateway
# genuinely produced nothing usable - not on a successful reply). Mirrors
# services/_shared/llm_client.py's Backend.POOLSIDE/OPENROUTER wire shape
# (same OpenAI-compatible /v1/chat/completions endpoints, same default
# models, same enable_thinking=false switch on Poolside) but NOT its
# multi-minute review-scale timeout/retry budget - a Discord reply needs to
# feel close to instant even in the failure case, per a past incident
# (Poolside's laguna-m.1 defaults to thinking ON: an early, unbounded config
# blew a 30s read timeout - measured 72s for even a tiny prompt - and each
# review call could still retry 3x on top of that; see grug's
# `_saas_overload_fallback_config` for the same lesson learned the hard way
# on the review side). Both tiers here are SINGLE-SHOT: one POST, no retry,
# no backoff - a 429/503 falls straight through to the next tier instead of
# spending time re-hitting the one that just said no.
#
# Timeout math (mirrors the worked-example comment style on grug's
# `_SAAS_OVERLOAD_FALLBACK_TIMEOUT_SECONDS`):
#   - Primary (query_ollama_api above): prod OLLAMA_URLS is the single
#     spark-gateway URL (k8s/deployment.yaml) under one _ollama_budget_s()
#     deadline, default 60s - typically done by 60s, 120s at the very
#     worst (see the budget comment above; more only if
#     GRUGTHINK_OLLAMA_TIMEOUT_S raises it), unchanged by this chain.
#   - Each fallback tier below: (5, 10) - 5s to connect, 10s to read - a
#     15s worst case per backend. Generous headroom over the observed live
#     latency (Poolside thinking-disabled + OpenRouter Haiku 4.5 both
#     measured well under 1s on grug's Elder path) while staying an order
#     of magnitude short of grug review's 330-350s scale, appropriate for a
#     realtime chat reply rather than a durable background job.
#   - Total worst case if EVERYTHING fails: 120s (primary) + 15s (Poolside)
#     + 15s (OpenRouter) [+ 30s Gemini bonus tier, query_gemini_api's own
#     existing request_options timeout, if GEMINI_API_KEY is configured]
#     = 150s (180s with Gemini). No SQS/job-timeout ceiling applies here
#     (unlike grug's review chain) - the operative bound is Discord's own
#     interaction-followup window (15 minutes), which this stays 5x inside
#     of even in the total-failure case.
_FALLBACK_TIMEOUT = (5, 10)  # (connect, read) seconds - see math above.

_POOLSIDE_URL = "https://inference.poolside.ai/v1/chat/completions"
_POOLSIDE_MODEL = "poolside/laguna-m.1"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"


def _extract_chat_message(body: dict) -> str:
    """The assistant message text from an OpenAI-compatible chat-completions
    body, or "" if the shape is missing/malformed. Pure - split out of
    _query_saas_fallback (grug#632) purely to shed one branch from that
    function's own complexity score; no behavior change."""
    choices = body.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return ((choices[0].get("message") or {}).get("content") or "").strip()
    return ""


def _query_saas_fallback(
    backend: str,
    url: str,
    model: str,
    api_key: str | None,
    extra_body: dict[str, Any],
    prompt_text: str,
    cache_key: str,
    server_db=None,
    personality_name: str = None,
    bot_id: str = None,
) -> str | None:
    """Shared single-shot OpenAI-compatible chat-completions call used by
    both query_poolside_api and query_openrouter_api - they differ only in
    URL/model/key/extra_body, so the transport + logging + LLM Obs shape
    lives here once rather than duplicated per backend."""
    # Import here to avoid circular dependency (same reason query_ollama_api
    # and query_gemini_api import it lazily above).
    from .prompts import validate_and_process_response

    if not api_key:
        log.warning(
            "saas_fallback_skipped_not_configured",
            extra={"backend": backend, "bot_id": bot_id, "personality": personality_name, "cache_key": cache_key},
        )
        return None

    span_tags = {"bot_id": str(bot_id or ""), "personality": str(personality_name or "")}
    start_ns = time.monotonic_ns()
    with _llmobs_llm(model_name=model, model_provider=backend, name=_LLMOBS_NAME) as span:
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt_text}],
                **extra_body,
            }
            headers = {"Authorization": f"Bearer {api_key}"}
            # Single-shot: exactly one POST, no retry loop, no backoff - see
            # the module-level timeout comment above for the full math.
            r = session.post(url, json=payload, headers=headers, timeout=_FALLBACK_TIMEOUT)
            if r.status_code == 200:
                response = _extract_chat_message(r.json())
                log.info(
                    "saas_fallback_response_received",
                    extra={
                        "bot_id": bot_id,
                        "personality": personality_name,
                        "backend": backend,
                        "model": model,
                        "response_length": len(response),
                        "cache_key": cache_key,
                    },
                )
                _llmobs_annotate(
                    span=span,
                    input_data=prompt_text,
                    output_data=response,
                    metadata={"backend": backend, "model": model, "status_code": r.status_code},
                    metrics={"latency_ms": _elapsed_ms(start_ns)},
                    tags=span_tags,
                )
                validated = validate_and_process_response(response, cache_key, server_db, personality_name, bot_id)
                if validated:
                    return validated
            else:
                log.warning(
                    "saas_fallback_http_error",
                    extra={"backend": backend, "model": model, "status_code": r.status_code, "bot_id": bot_id},
                )
                _llmobs_annotate(
                    span=span,
                    input_data=prompt_text,
                    metadata={"backend": backend, "model": model, "error": f"http_{r.status_code}"},
                    metrics={"latency_ms": _elapsed_ms(start_ns)},
                    tags=span_tags,
                )
        except requests.exceptions.Timeout as e:
            log.error(
                "saas_fallback_timeout",
                extra={"backend": backend, "model": model, "bot_id": bot_id, "error": str(e)},
            )
            _llmobs_annotate(
                span=span,
                input_data=prompt_text,
                metadata={"backend": backend, "model": model, "error": "Timeout"},
                metrics={"latency_ms": _elapsed_ms(start_ns)},
                tags=span_tags,
            )
        except requests.exceptions.ConnectionError as e:
            log.error(
                "saas_fallback_connection_failed",
                extra={"backend": backend, "model": model, "bot_id": bot_id, "error": str(e)},
            )
            _llmobs_annotate(
                span=span,
                input_data=prompt_text,
                metadata={"backend": backend, "model": model, "error": "ConnectionError"},
                metrics={"latency_ms": _elapsed_ms(start_ns)},
                tags=span_tags,
            )
        except requests.exceptions.RequestException as e:
            log.error(
                "saas_fallback_request_failed",
                extra={
                    "backend": backend,
                    "model": model,
                    "bot_id": bot_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            _llmobs_annotate(
                span=span,
                input_data=prompt_text,
                metadata={"backend": backend, "model": model, "error": type(e).__name__},
                metrics={"latency_ms": _elapsed_ms(start_ns)},
                tags=span_tags,
            )
        except Exception as e:
            log.error(
                "saas_fallback_unexpected_error",
                extra={
                    "backend": backend,
                    "model": model,
                    "bot_id": bot_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            _llmobs_annotate(
                span=span,
                input_data=prompt_text,
                metadata={"backend": backend, "model": model, "error": type(e).__name__},
                metrics={"latency_ms": _elapsed_ms(start_ns)},
                tags=span_tags,
            )
    return None


def query_poolside_api(
    prompt_text: str, cache_key: str, server_db=None, personality_name: str = None, bot_id: str = None
) -> str | None:
    """First fallback tier, tried only after query_ollama_api fails. Single-
    shot, short-timeout - see the module-level comment above _FALLBACK_TIMEOUT
    for the full worst-case-time math.

    Returns:
        Validated response string, or None if not configured / call failed.
    """
    return _query_saas_fallback(
        backend="poolside",
        url=_POOLSIDE_URL,
        model=_POOLSIDE_MODEL,
        api_key=config.POOLSIDE_API_KEY,
        # Poolside's laguna-m.1 defaults to thinking ON - disables it so the
        # reply lands well inside _FALLBACK_TIMEOUT instead of spending the
        # whole budget on hidden reasoning tokens (same fix grug's Elder
        # applies on its own Poolside config, measured 72s->under 1s live).
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        prompt_text=prompt_text,
        cache_key=cache_key,
        server_db=server_db,
        personality_name=personality_name,
        bot_id=bot_id,
    )


def query_openrouter_api(
    prompt_text: str, cache_key: str, server_db=None, personality_name: str = None, bot_id: str = None
) -> str | None:
    """Second fallback tier, tried only after query_ollama_api AND
    query_poolside_api both fail. Single-shot, short-timeout - see the
    module-level comment above _FALLBACK_TIMEOUT for the full worst-case-
    time math.

    Deliberately uses the fast default Haiku 4.5 model, NOT the Opus-plus-
    high-reasoning override grug's Elder review persona configures via
    GRUG_OPENROUTER_REVIEW_MODEL - that combination is tuned for a multi-
    minute deep-review pass and is unsuited to a realtime chat reply.

    Returns:
        Validated response string, or None if not configured / call failed.
    """
    return _query_saas_fallback(
        backend="openrouter",
        url=_OPENROUTER_URL,
        model=_OPENROUTER_MODEL,
        api_key=config.OPENROUTER_API_KEY,
        extra_body={},
        prompt_text=prompt_text,
        cache_key=cache_key,
        server_db=server_db,
        personality_name=personality_name,
        bot_id=bot_id,
    )


def query_gemini_api(
    prompt_text: str, cache_key: str, server_db=None, personality_name: str = None, bot_id: str = None
) -> str | None:
    """Query Google Gemini API for LLM response.

    Args:
        prompt_text: The prompt to send to the model
        cache_key: Cache key for tracking this request
        server_db: Server database instance for storing lore
        personality_name: Name of the bot personality
        bot_id: Unique identifier for this bot instance

    Returns:
        Validated response string, error message, or None if validation failed
    """
    # Import here to avoid circular dependency
    from .prompts import validate_and_process_response

    log.info(
        "Starting Gemini API query",
        extra={
            "bot_id": bot_id,
            "personality": personality_name,
            "model": config.GEMINI_MODEL,
            "prompt_length": len(prompt_text),
            "cache_key": cache_key,
        },
    )
    try:
        # Check if API key is configured
        if not config.GEMINI_API_KEY:
            log.error("Gemini API key not configured", extra={"bot_id": bot_id})
            return "Error: Gemini API key not configured. Please set GEMINI_API_KEY environment variable."

        import google.generativeai as genai

        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(model_name=config.GEMINI_MODEL)
        resp = model.generate_content(
            prompt_text,
            stream=False,
            generation_config={"temperature": 0.3, "top_p": 0.5},
            request_options={"timeout": 30},
        )

        log.info(
            "Gemini API response received",
            extra={
                "bot_id": bot_id,
                "personality": personality_name,
                "response_length": len(resp.text) if resp.text else 0,
                "cache_key": cache_key,
            },
        )

        validated = validate_and_process_response(resp.text, cache_key, server_db, personality_name, bot_id)
        if validated:
            return validated
    except ImportError as e:
        log.error(
            "Gemini library not available",
            extra={"bot_id": bot_id, "personality": personality_name, "error": str(e), "cache_key": cache_key},
        )
        return "Error: google-generativeai library not installed. Please install it or switch to Ollama."
    except Exception as e:
        log.error(
            "Gemini API call failed",
            extra={
                "bot_id": bot_id,
                "personality": personality_name,
                "error": str(e),
                "error_type": type(e).__name__,
                "cache_key": cache_key,
            },
        )
        return f"Error: Gemini API call failed - {str(e)}"
    return None
