"""LLM API client functions for GrugThink bot.

This module handles communication with different LLM backends:
- Ollama API (local/self-hosted models)
- Google Gemini API (cloud-based models)
"""

import requests

from .. import config_legacy as config
from ..logging_config import get_logger

log = get_logger(__name__)

# Shared requests session for connection pooling
session = requests.Session()


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
            r = session.post(f"{url}/api/generate", json=payload, timeout=(10, 60))
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
                validated = validate_and_process_response(response, cache_key, server_db, personality_name, bot_id)
                if validated:
                    return validated
            else:
                log.warning(
                    "Ollama API returned error",
                    extra={"bot_id": bot_id, "url": url, "status_code": r.status_code, "model": raw_model},
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
        except requests.exceptions.ConnectionError as e:
            log.error(
                "Ollama connection failed",
                extra={"bot_id": bot_id, "url": url, "model": raw_model, "error": str(e)},
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
    return None


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
