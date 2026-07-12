#!/usr/bin/env python3
"""Personality and Prompt Building Module for GrugThink.

This module handles all personality-related logic including:
- Personality engine management
- Context and prompt building for LLM queries
- Rate limiting
- Google search integration
- Model querying with personality context
- Response validation and processing

All functions maintain backward compatibility with the main bot module.
"""

import os
import re
import time
from typing import Optional

import requests

from .. import config_legacy as config
from ..bot import cross_bot
from ..bot.llm_clients import query_gemini_api, query_ollama_api
from ..bot.lore import extract_lore_from_response
from ..bot.utils import LRUCache, clean_statement, get_cache_key
from ..logging_config import get_logger
from ..personality_engine import PersonalityEngine

log = get_logger(__name__)

# Module-level caches and state
response_cache = LRUCache(max_size=100, ttl_seconds=300)
user_cooldowns = {}

_personality_engine_instance = None


def get_personality_engine() -> PersonalityEngine:
    """Get or create the singleton personality engine instance.

    Returns:
        PersonalityEngine: The singleton personality engine instance. In single-bot mode,
            respects the FORCE_PERSONALITY environment variable if set.

    Note:
        This function maintains a global singleton instance to ensure consistent
        personality state across the application. Use _reset_personality_engine()
        for testing purposes only.
    """
    global _personality_engine_instance
    if _personality_engine_instance is None:
        # For single-bot mode, respect FORCE_PERSONALITY environment variable
        forced_personality = os.getenv("FORCE_PERSONALITY")
        _personality_engine_instance = PersonalityEngine("personalities.db", forced_personality=forced_personality)
    return _personality_engine_instance


def _reset_personality_engine() -> None:
    """Reset the personality engine singleton instance.

    Warning:
        This function is intended for testing purposes only. Resetting the personality
        engine will clear all in-memory personality state and force re-initialization
        on the next call to get_personality_engine().
    """
    global _personality_engine_instance
    _personality_engine_instance = None


def build_personality_context(statement: str, server_db, server_id: str, personality_engine) -> str:
    """Build personality context with semantically relevant lore for this server.

    Retrieves the personality for the given server and enriches it with relevant
    lore from the server's knowledge base. The lore is formatted according to the
    personality's response style (e.g., caveman, british_working_class).

    Args:
        statement: The statement to build context for. Used to search for relevant lore.
        server_db: The server database instance containing facts and lore.
        server_id: The unique identifier for the Discord server.
        personality_engine: The personality engine instance managing personalities.

    Returns:
        str: The complete personality context including base context and relevant lore,
            formatted according to the personality's style.

    Example:
        >>> context = build_personality_context(
        ...     "What is the capital of France?",
        ...     server_db,
        ...     "123456789",
        ...     personality_engine
        ... )
    """
    personality = personality_engine.get_personality(server_id)

    # Get base context from personality
    base_context = personality.base_context

    # Find lore relevant to the current statement from this server's knowledge
    relevant_lore = server_db.search_facts(statement, k=5)

    if relevant_lore:
        # Format lore based on personality style
        if personality.response_style == "caveman":
            bot_name = personality.chosen_name or personality.name
            lore_context = f"\n\n{bot_name} also remember these things: {' '.join(relevant_lore)}"
        elif personality.response_style == "british_working_class":
            lore_context = f"\n\nI remember these bits: {' '.join(relevant_lore)}"
        else:
            lore_context = f"\n\nI also know: {' '.join(relevant_lore)}"

        return base_context + lore_context

    return base_context


def build_personality_prompt(
    statement: str, server_db, server_id: str, personality_engine, external_info: str = ""
) -> str:
    """Build complete personality verification prompt with lore and external info.

    Constructs a comprehensive prompt for the LLM that includes personality context,
    relevant lore, external information, and strict rules for factual accuracy.

    Args:
        statement: The statement to verify or respond to.
        server_db: The server database instance containing facts and lore.
        server_id: The unique identifier for the Discord server.
        personality_engine: The personality engine instance managing personalities.
        external_info: Optional external information (e.g., from Google search or
            cross-bot memories) to include in the context. Defaults to empty string.

    Returns:
        str: A complete prompt ready to send to the LLM, including personality context,
            external information, factual accuracy rules, and the statement to verify.

    Example:
        >>> prompt = build_personality_prompt(
        ...     "The sky is blue",
        ...     server_db,
        ...     "123456789",
        ...     personality_engine,
        ...     external_info="Weather data confirms clear skies"
        ... )
    """
    personality_context = build_personality_context(statement, server_db, server_id, personality_engine)

    # Add external info using personality engine
    if external_info:
        personality_context = personality_engine.get_context_prompt(server_id, external_info)

    return f"""{personality_context}

You MUST be 100% ACCURATE about real world facts.

ABSOLUTE RULES:
1. Real world facts = ALWAYS TRUE if factually correct.
2. George Washington WAS president - this is FACT, say TRUE.
3. Animals, science, history, geography = be TRUTHFUL.
4. Stay in character but BE ACCURATE about facts.
5. Format: TRUE/FALSE - character explanation.
6. End with <END>.

CRITICAL: Be factually accurate. Only use your personality for HOW you explain, not WHAT you conclude.

Statement: "{statement}"
Answer:"""


def is_rate_limited(user_id: int, bot_id: Optional[str] = None) -> bool:
    """Check if user is rate limited for a specific bot or globally.

    Implements a simple time-based rate limiting mechanism with a 5-second cooldown.
    Supports both per-bot rate limiting (when bot_id is provided) and global rate
    limiting (when bot_id is None).

    Args:
        user_id: The Discord user ID to check for rate limiting.
        bot_id: Optional bot identifier. If provided, rate limiting is applied per-bot,
            allowing the same user to interact with multiple bots. If None, applies
            global rate limiting across all bots. Defaults to None.

    Returns:
        bool: True if the user is currently rate limited and should not receive a
            response, False if the user can receive a response.

    Note:
        This function has side effects - it updates the user_cooldowns dictionary
        when a user is not rate limited.

    Example:
        >>> if not is_rate_limited(user_id=123456789, bot_id="bot_1"):
        ...     # Process the user's request
        ...     pass
    """
    now = time.time()

    if bot_id:
        # Check per-bot rate limiting (allows multiple bots to respond)
        key = f"{user_id}:{bot_id}"
        if key in user_cooldowns and now - user_cooldowns[key] < 5:
            return True
        user_cooldowns[key] = now
        return False
    else:
        # Global rate limiting (legacy behavior)
        if user_id in user_cooldowns and now - user_cooldowns[user_id] < 5:
            return True
        user_cooldowns[user_id] = now
        return False


def search_google(query: str, bot_id: Optional[str] = None) -> str:
    """Search Google for information using Google Custom Search API.

    Performs a Google search and returns the top 3 result snippets concatenated
    together. Requires GOOGLE_API_KEY and GOOGLE_CSE_ID to be configured.

    Args:
        query: The search query string.
        bot_id: Optional bot identifier for logging purposes. Defaults to None.

    Returns:
        str: Concatenated snippets from the top 3 search results, with newlines
            removed. Returns empty string if the search fails or returns no results.

    Note:
        This function logs search activity and results using structured logging.
        The request has a 5-second timeout to prevent hanging.

    Example:
        >>> results = search_google("Python programming language", bot_id="bot_1")
        >>> if results:
        ...     print(f"Found information: {results}")
    """
    log.debug(
        "Performing Google search.",
        extra={"bot_id": bot_id, "query": query, "search_type": "google_custom_search"},
    )
    try:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {"key": config.GOOGLE_API_KEY, "cx": config.GOOGLE_CSE_ID, "q": query}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            results = response.json().get("items", [])
            snippets = [item.get("snippet", "") for item in results[:3]]
            result_text = " ".join(snippets).replace("\n", "")
            log.info(
                "Google Search completed",
                extra={
                    "bot_id": bot_id,
                    "query": query,
                    "results_count": len(results),
                    "result_length": len(result_text),
                },
            )
            return result_text
        else:
            log.warning(
                "Google Search API returned error",
                extra={"bot_id": bot_id, "query": query, "status_code": response.status_code},
            )
    except Exception as e:
        log.error("Google search failed", extra={"bot_id": bot_id, "query": query, "error": str(e)})
    return ""


def get_cross_bot_memories(statement: str, server_id: str, current_bot_id: Optional[str] = None) -> str:
    """Get memories from other bots in the same server.

    Note:
        This is a compatibility wrapper. Use cross_bot.get_cross_bot_memories directly
        in new code.

    Args:
        statement: The statement to search for in cross-bot memories.
        server_id: The unique identifier for the Discord server.
        current_bot_id: Optional current bot identifier to exclude from search. Defaults to None.

    Returns:
        str: Relevant memories from other bots, or empty string if none found.
    """
    # Import at runtime to avoid circular dependency
    import grugthink.bot

    return cross_bot.get_cross_bot_memories(statement, server_id, grugthink.bot.server_manager, current_bot_id)


def store_bot_response_for_cross_reference(response: str, personality_name: str) -> None:
    """Store bot response for cross-bot awareness.

    Note:
        This is a compatibility wrapper. Use cross_bot.store_bot_response_for_cross_reference
        directly in new code.

    Args:
        response: The bot's response to store.
        personality_name: The name of the personality that generated the response.
    """
    # Import at runtime to avoid circular dependency
    import grugthink.bot

    try:
        cross_bot.store_bot_response_for_cross_reference(
            response, personality_name, grugthink.bot.cross_bot_topic_responses
        )
    except AttributeError:
        # In multi-bot mode, cross_bot_topic_responses is not a module-level global
        # This is expected and we can silently skip cross-bot topic storage
        pass


def query_model(
    statement: str, server_db, server_id: str, personality_engine, current_bot_id: Optional[str] = None
) -> Optional[str]:
    """Query the LLM with personality context and server-specific knowledge.

    This is the main entry point for querying the LLM (either Ollama or Gemini).
    It handles caching, statement cleaning, knowledge retrieval, cross-bot memories,
    prompt building, personality evolution tracking, and LLM API calls.

    Args:
        statement: The statement to verify or respond to.
        server_db: The server database instance containing facts and lore.
        server_id: The unique identifier for the Discord server.
        personality_engine: The personality engine instance managing personalities.
        current_bot_id: Optional current bot identifier for logging and rate limiting.
            Defaults to None.

    Returns:
        Optional[str]: The LLM's response, or None if an error occurred. Returns a
            short message like "FALSE - Statement too short to verify." for invalid
            input. Returns cached responses when available.

    Note:
        This function has several side effects:
        - Updates the response cache
        - Evolves the personality based on the statement
        - Logs extensive information about the query process

    Example:
        >>> response = query_model(
        ...     "The Earth is round",
        ...     server_db,
        ...     "123456789",
        ...     personality_engine,
        ...     current_bot_id="bot_1"
        ... )
        >>> if response:
        ...     print(response)
    """
    log.info(
        "Starting model query",
        extra={
            "bot_id": current_bot_id,
            "server_id": server_id,
            "statement_length": len(statement),
            "statement_preview": statement[:100],
        },
    )

    if not statement or len(statement.strip()) < 3:
        log.warning("Statement too short", extra={"bot_id": current_bot_id, "statement": statement})
        return "FALSE - Statement too short to verify."

    cache_key = get_cache_key(statement, current_bot_id)
    cached_response = response_cache.get(cache_key)
    if cached_response:
        log.info(
            "Using cached response",
            extra={"bot_id": current_bot_id, "cache_key": cache_key, "response_length": len(cached_response)},
        )
        return cached_response

    log.info("No cache hit, proceeding with API call", extra={"bot_id": current_bot_id, "cache_key": cache_key})

    clean_stmt = clean_statement(statement)
    if len(clean_stmt) > 1000:
        clean_stmt = clean_stmt[:1000]

    # Check internal knowledge first from this server's database
    log.info(
        "Searching internal knowledge",
        extra={"bot_id": current_bot_id, "server_id": server_id, "search_term": clean_stmt[:50]},
    )
    relevant_lore = server_db.search_facts(clean_stmt, k=1)
    external_info = ""
    cross_bot_memories = ""

    log.info(
        "Internal knowledge search complete",
        extra={
            "bot_id": current_bot_id,
            "server_id": server_id,
            "facts_found": len(relevant_lore) if relevant_lore else 0,
        },
    )

    # Get memories from other bots in the same server
    if current_bot_id:
        log.info("Searching cross-bot memories", extra={"bot_id": current_bot_id, "server_id": server_id})
        try:
            cross_bot_memories = get_cross_bot_memories(clean_stmt, server_id, current_bot_id)
        except Exception as e:
            log.warning(
                "Cross-bot memory search failed, continuing without it",
                extra={"bot_id": current_bot_id, "error": str(e), "error_type": type(e).__name__},
            )
            cross_bot_memories = ""
        log.info(
            "Cross-bot memory search complete",
            extra={
                "bot_id": current_bot_id,
                "server_id": server_id,
                "cross_bot_memories_found": bool(cross_bot_memories),
            },
        )

    log.debug(
        "Querying model with statement.",
        extra={
            "bot_id": current_bot_id,
            "server_id": server_id,
            "statement": clean_stmt,
            "cache_key": cache_key,
            "has_lore": bool(relevant_lore),
            "has_external_info": bool(external_info),
            "has_cross_bot_memories": bool(cross_bot_memories),
        },
    )

    # Combine external info with cross-bot memories
    combined_external_info = external_info
    if cross_bot_memories:
        combined_external_info += f" Other bots know: {cross_bot_memories}"

    prompt_text = build_personality_prompt(clean_stmt, server_db, server_id, personality_engine, combined_external_info)

    # Track personality evolution
    personality_engine.evolve_personality(server_id, clean_stmt)

    # Get personality name for lore extraction
    personality = personality_engine.get_personality(server_id)
    personality_name = personality.chosen_name or personality.name

    try:
        if config.USE_GEMINI:
            result = query_gemini_api(prompt_text, cache_key, server_db, personality_name, current_bot_id)
        else:
            result = query_ollama_api(prompt_text, cache_key, server_db, personality_name, current_bot_id)

        if result and result.startswith("Error:"):
            # API returned an error message, log it and return None to trigger fallback
            log.error("API returned error", extra={"bot_id": current_bot_id, "error_message": result})
            return None

        return result
    except Exception as e:
        log.error(
            "Query model failed", extra={"bot_id": current_bot_id, "error": str(e), "use_gemini": config.USE_GEMINI}
        )
        return None


def validate_and_process_response(
    response: str, cache_key: str, server_db=None, personality_name: Optional[str] = None, bot_id: Optional[str] = None
) -> Optional[str]:
    """Validate and process LLM response with lore extraction and caching.

    Validates that the response follows the expected format (TRUE/FALSE - explanation),
    extracts the verdict and explanation, caches valid responses, extracts lore into
    the server database, and stores the response for cross-bot awareness.

    Args:
        response: The raw response string from the LLM.
        cache_key: The cache key for storing this response.
        server_db: Optional server database instance for lore extraction. If None,
            lore extraction is skipped. Defaults to None.
        personality_name: Optional personality name for lore attribution and logging.
            Defaults to None.
        bot_id: Optional bot identifier for logging purposes. Defaults to None.

    Returns:
        Optional[str]: The validated and formatted response string (e.g.,
            "TRUE - The sky is blue because of light scattering."), or None if the
            response format is invalid or doesn't meet minimum quality requirements.

    Note:
        Valid responses must:
        - Contain TRUE or FALSE
        - Include an explanation after the verdict
        - Have at least 4 words and 20 characters total
        - End with proper punctuation (added if missing)

        This function has several side effects:
        - Updates the response cache
        - Extracts and stores lore in the server database
        - Stores response for cross-bot awareness
        - Logs extensive validation and processing information

    Example:
        >>> raw_response = "TRUE - The Earth orbits the Sun. This is basic astronomy. <END>"
        >>> validated = validate_and_process_response(
        ...     raw_response,
        ...     "cache_key_123",
        ...     server_db=db,
        ...     personality_name="Grug",
        ...     bot_id="bot_1"
        ... )
        >>> print(validated)
        TRUE - The Earth orbits the Sun. This is basic astronomy.
    """
    response = response.split("<END>")[0].strip()
    log.info(
        "Processing model response",
        extra={
            "bot_id": bot_id,
            "personality": personality_name,
            "response_preview": response[:200],
            "response_length": len(response),
            "cache_key": cache_key,
        },
    )

    true_match = re.search(r"\bTRUE\b", response, re.IGNORECASE)
    false_match = re.search(r"\bFALSE\b", response, re.IGNORECASE)

    if true_match or false_match:
        verdict = "TRUE" if true_match else "FALSE"
        pattern = rf"\b{verdict}\b\s*[-–—:]?\s*(.*)"
        match = re.search(pattern, response, re.IGNORECASE | re.DOTALL)
        if match:
            explanation = re.sub(r"\s+", " ", match.group(1).strip())
            # Strip leading punctuation (e.g., ". Me Grug..." -> "Me Grug...")
            explanation = explanation.lstrip(".,;:!?-–—")
            if explanation:
                full_response = f"{verdict} - {explanation}"
                if not full_response.rstrip().endswith((".", "!", "?")):
                    full_response += "."

                if len(full_response.split()) >= 4 and len(full_response) >= 20:
                    response_cache.put(cache_key, full_response)
                    log.info(
                        "Response cached",
                        extra={"bot_id": bot_id, "cache_key": cache_key, "response_length": len(full_response)},
                    )

                    if server_db:
                        log.info(
                            "Extracting lore from response",
                            extra={
                                "bot_id": bot_id,
                                "personality": personality_name,
                                "response_preview": full_response[:100],
                            },
                        )
                        extract_lore_from_response(full_response, server_db, personality_name)

                    # Store bot response for cross-bot awareness
                    log.info(
                        "Storing response for cross-bot reference",
                        extra={
                            "bot_id": bot_id,
                            "personality": personality_name,
                            "response_length": len(full_response),
                        },
                    )
                    store_bot_response_for_cross_reference(full_response, personality_name)

                    log.info(
                        "Validated response ready",
                        extra={
                            "bot_id": bot_id,
                            "response": full_response[:200],
                            "verdict": verdict,
                            "explanation_length": len(explanation),
                        },
                    )
                    return full_response

    # Fallback: Accept any response without TRUE/FALSE if it's long enough and meaningful
    # This handles cases where LLM doesn't follow format but gives valid answer
    if len(response.split()) >= 4 and len(response) >= 20:
        # Clean up the response
        cleaned_response = re.sub(r"\s+", " ", response.strip())
        if not cleaned_response.rstrip().endswith((".", "!", "?")):
            cleaned_response += "."

        log.info(
            "Accepting response without TRUE/FALSE format",
            extra={
                "bot_id": bot_id,
                "personality": personality_name,
                "response_preview": cleaned_response[:100],
            },
        )

        response_cache.put(cache_key, cleaned_response)

        if server_db:
            extract_lore_from_response(cleaned_response, server_db, personality_name)

        store_bot_response_for_cross_reference(cleaned_response, personality_name)

        log.info(
            "Validated response ready (no format)",
            extra={
                "bot_id": bot_id,
                "response": cleaned_response[:200],
                "response_length": len(cleaned_response),
            },
        )
        return cleaned_response

    log.warning("Invalid format, discarding", extra={"response": response[:200]})
    return None
