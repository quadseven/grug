import os
import re

# --- Discord Configuration ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Only validate Discord token in single-bot mode (when not in multi-bot container or OAuth disabled)
# Multi-bot mode will set environment variables dynamically per bot instance
# OAuth disabled mode allows API-only operation without Discord bot
if not DISCORD_TOKEN and not os.getenv("GRUGTHINK_MULTIBOT_MODE") and not os.getenv("DISABLE_OAUTH"):
    raise ValueError("Missing DISCORD_TOKEN")

# --- Google Search Configuration ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")

# --- GrugBot Configuration ---
DATA_DIR = os.getenv("GRUGBOT_DATA_DIR", os.path.dirname(__file__))


# Create unique database path based on Discord token to prevent memory sharing between bot instances
def _get_unique_db_path():
    if DISCORD_TOKEN:
        # Create a short hash of the Discord token for database isolation
        import hashlib

        token_hash = hashlib.sha256(DISCORD_TOKEN.encode()).hexdigest()[:12]
        return os.path.join(DATA_DIR, f"grug_lore_{token_hash}.db")
    else:
        # Fallback for multi-bot mode or when token not available
        return os.path.join(DATA_DIR, "grug_lore.db")


DB_PATH = _get_unique_db_path()
GRUGBOT_VARIANT = os.getenv("GRUGBOT_VARIANT", "prod")
TRUSTED_USER_IDS = [int(uid) for uid in os.getenv("TRUSTED_USER_IDS", "").split(",") if uid.strip()]

# Whether to load the heavy embedding model at startup. Set to 'False' for lightweight deployments.
LOAD_EMBEDDER = os.getenv("LOAD_EMBEDDER", "True").lower() == "true"

# --- Logging Configuration ---
LOG_LEVEL_STR = os.getenv("LOG_LEVEL", "INFO").upper()


# --- Validation ---
def is_valid_url(url):
    # More robust regex for URL validation
    regex = re.compile(
        r"^(?:http)s?://"  # http:// or https://
        r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|"  # domain...
        r"localhost|"  # localhost...
        r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"  # ...or ip
        r"(?::\d+)?"  # optional port
        r"(?:/?|[/?]\S+)",
        re.IGNORECASE,
    )
    return re.match(regex, url) is not None


# --- LLM Configuration ---
# CRITICAL FIX: Use __getattr__ to lazy-load these values from os.environ at RUNTIME
# This fixes Issue #47 where multi-bot mode sets environment variables AFTER module import
#
# Problem: In multi-bot mode, lifecycle.py sets os.environ["OLLAMA_URLS"] = "http://..." AFTER
# config_legacy.py has already been imported. If we cache OLLAMA_URLS at import time, it will
# be empty and query_ollama_api() will hang (empty for loop, returns None, no error).
#
# Solution: Use Python 3.7+ module __getattr__ to read from os.environ on every access.


def __getattr__(name):
    """Lazy-load configuration values from environment at runtime.

    This allows multi-bot mode to set environment variables AFTER module import.
    Without this, cached values would be empty/defaults from import time.
    """
    import logging

    _log = logging.getLogger(__name__)

    if name == "OLLAMA_URLS":
        raw_value = os.getenv("OLLAMA_URLS", "")
        urls = [url.strip() for url in raw_value.split(",") if url.strip()]

        # Log what we're returning for debugging
        _log.debug(
            "Loading OLLAMA_URLS from environment",
            extra={
                "raw_env_value": raw_value,
                "parsed_urls": urls,
                "count": len(urls),
                "source": "os.environ" if "OLLAMA_URLS" in os.environ else "default",
            },
        )

        # Validate URLs (only if we have URLs to validate)
        if urls:
            for url in urls:
                if not is_valid_url(url):
                    raise ValueError(f"Invalid OLLAMA_URL: {url}")
        else:
            # CRITICAL: Log when OLLAMA_URLS is empty - this is likely a configuration error
            _log.warning(
                "OLLAMA_URLS is empty - LLM queries via Ollama will fail",
                extra={"raw_env_value": raw_value, "parsed_urls": urls},
            )

        return urls

    elif name == "OLLAMA_MODELS":
        raw_value = os.getenv("OLLAMA_MODELS", "llama3.2:3b")
        models = [model.strip() for model in raw_value.split(",") if model.strip()]

        _log.debug(
            "Loading OLLAMA_MODELS from environment",
            extra={"raw_env_value": raw_value, "parsed_models": models, "count": len(models)},
        )

        # Validate model names
        for model in models:
            if not re.match(r"^[\w\-\.:]+$", model):
                raise ValueError(f"Invalid model name: {model}")
        return models

    elif name == "GEMINI_API_KEY":
        key = os.getenv("GEMINI_API_KEY")
        if key and not re.match(r"^[\w\-]+$", key):
            raise ValueError("Invalid GEMINI_API_KEY")
        return key

    elif name == "GEMINI_MODEL":
        return os.getenv("GEMINI_MODEL", "gemini-pro")

    elif name == "USE_GEMINI":
        # Dynamically compute based on current GEMINI_API_KEY value
        return bool(__getattr__("GEMINI_API_KEY"))

    elif name == "CAN_SEARCH":
        return bool(GOOGLE_API_KEY and GOOGLE_CSE_ID)

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def log_initial_settings():
    """Log initial configuration settings for debugging."""
    import logging

    logger = logging.getLogger(__name__)
    logger.info(
        "Configuration loaded",
        extra={
            "variant": GRUGBOT_VARIANT,
            "use_gemini": __getattr__("USE_GEMINI"),
            "can_search": __getattr__("CAN_SEARCH"),
            "trusted_users_count": len(TRUSTED_USER_IDS),
            "log_level": LOG_LEVEL_STR,
        },
    )
