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

    for idx, url in enumerate(config.OLLAMA_URLS):
        raw_model = config.OLLAMA_MODELS[idx] if idx < len(config.OLLAMA_MODELS) else config.OLLAMA_MODELS[0]
        span_tags = {"bot_id": str(bot_id or ""), "personality": str(personality_name or "")}
        start_ns = time.monotonic_ns()
        with _llmobs_llm(model_name=raw_model, model_provider="ollama", name=_LLMOBS_NAME) as span:
            try:
                payload = {
                    "model": raw_model,
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
                # (connect, read). Read raised 30->60s: the 122B chat model is slower
                # than the old 3B default even with thinking off.
                #
                # X-Spark-Priority (githumps/infra#1768/#1770/#1773): this URL is
                # the in-cluster spark-gateway (OLLAMA_URLS is set to it in
                # k8s/deployment.yaml, not to the Sparks directly) - a Discord
                # reply is latency-sensitive and must never queue behind one of
                # Hermes's long agentic turns on the shared Ollama target.
                # "realtime" (not "interactive"): live incident 2026-07-13 -
                # this call queued behind Grug's OWN code-review calls (both
                # tagged "interactive", FIFO within the tier put chat second)
                # for 24+ minutes with no client-side timeout ever firing (the
                # gateway's queue-wait heartbeat kept resetting it). A stalled
                # Discord reply reads as "the bot is broken" within seconds, so
                # it needs to win over Grug's own async review work, not just
                # over Hermes's batch turns.
                # X-Spark-Caller identifies this consumer in the gateway's own
                # metrics/dashboard instead of falling back to a generic
                # "python (ip)" UA guess. Harmless if OLLAMA_URLS ever points
                # straight at a Spark instead - Ollama ignores unknown headers.
                headers = {"X-Spark-Priority": "realtime", "X-Spark-Caller": "grugthink-chat"}
                r = session.post(f"{url}/api/generate", json=payload, headers=headers, timeout=(10, 60))
                if r.status_code == 200:
                    response = r.json().get("response", "").strip()
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
                        metadata={"model": raw_model, "url": url, "status_code": r.status_code},
                        metrics={"latency_ms": _elapsed_ms(start_ns)},
                        tags=span_tags,
                    )
                    validated = validate_and_process_response(response, cache_key, server_db, personality_name, bot_id)
                    if validated:
                        return validated
                else:
                    log.warning(
                        "Ollama API returned error",
                        extra={"bot_id": bot_id, "url": url, "status_code": r.status_code, "model": raw_model},
                    )
                    _llmobs_annotate(
                        span=span,
                        input_data=prompt_text,
                        metadata={"model": raw_model, "url": url, "error": f"http_{r.status_code}"},
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
                        "timeout": "30s read, 10s connect",
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
#     spark-gateway URL (k8s/deployment.yaml) with timeout=(10, 60) - one
#     70s-worst-case attempt, unchanged by this fallback chain.
#   - Each fallback tier below: (5, 10) - 5s to connect, 10s to read - a
#     15s worst case per backend. Generous headroom over the observed live
#     latency (Poolside thinking-disabled + OpenRouter Haiku 4.5 both
#     measured well under 1s on grug's Elder path) while staying an order
#     of magnitude short of grug review's 330-350s scale, appropriate for a
#     realtime chat reply rather than a durable background job.
#   - Total worst case if EVERYTHING fails: 70s (primary) + 15s (Poolside)
#     + 15s (OpenRouter) [+ 30s Gemini bonus tier, query_gemini_api's own
#     existing request_options timeout, if GEMINI_API_KEY is configured]
#     = 100s (130s with Gemini). No SQS/job-timeout ceiling applies here
#     (unlike grug's review chain) - the operative bound is Discord's own
#     interaction-followup window (15 minutes), which this stays two
#     orders of magnitude inside of even in the total-failure case.
_FALLBACK_TIMEOUT = (5, 10)  # (connect, read) seconds - see math above.

_POOLSIDE_URL = "https://inference.poolside.ai/v1/chat/completions"
_POOLSIDE_MODEL = "poolside/laguna-m.1"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"


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
                body = r.json()
                choices = body.get("choices") or []
                response = ""
                if choices and isinstance(choices[0], dict):
                    response = ((choices[0].get("message") or {}).get("content") or "").strip()
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
