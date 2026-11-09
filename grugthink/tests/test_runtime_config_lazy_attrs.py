"""Tests for runtime_config.py's __getattr__ dispatch (grug#632).

Imports `runtime_config` directly rather than through `src.grugthink.config`,
which resolves to the multi-bot config package, not this file.

Each attribute is read fresh via __getattr__ on every access (no caching),
so these tests vary os.environ per-case against ONE module import rather
than reloading the module.
"""

import os

# runtime_config.py raises at IMPORT time if DISCORD_TOKEN is unset - same
# guard test_llm_fallback.py uses ahead of its own module-level import.
os.environ.setdefault("DISCORD_TOKEN", "fake_token")

import pytest  # noqa: E402

from src.grugthink import bot_settings, runtime_config  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in (
        "OLLAMA_URLS", "OLLAMA_MODELS", "GEMINI_API_KEY", "GEMINI_MODEL",
        "POOLSIDE_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CSE_ID",
        "TRUSTED_USER_IDS",
    ):
        monkeypatch.delenv(var, raising=False)


class TestOllamaUrls:
    def test_default_is_empty_and_logs_a_warning(self, caplog):
        with caplog.at_level("WARNING"):
            assert runtime_config.OLLAMA_URLS == []
        assert "OLLAMA_URLS is empty" in caplog.text

    def test_parses_comma_separated_list(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_URLS", "http://localhost:11434, http://example.com:11434")
        assert runtime_config.OLLAMA_URLS == ["http://localhost:11434", "http://example.com:11434"]

    def test_invalid_url_raises(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_URLS", "not-a-valid-url")
        with pytest.raises(ValueError, match="Invalid OLLAMA_URL"):
            _ = runtime_config.OLLAMA_URLS


class TestOllamaModels:
    def test_default(self):
        assert runtime_config.OLLAMA_MODELS == ["llama3.2:3b"]

    def test_parses_comma_separated_list(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_MODELS", "llama3.2:3b,grug:latest")
        assert runtime_config.OLLAMA_MODELS == ["llama3.2:3b", "grug:latest"]

    def test_invalid_model_name_raises(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_MODELS", "invalid/model name")
        with pytest.raises(ValueError, match="Invalid model name"):
            _ = runtime_config.OLLAMA_MODELS


class TestGeminiApiKey:
    def test_unset_is_none(self):
        assert runtime_config.GEMINI_API_KEY is None

    def test_valid_key(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "abc-123")
        assert runtime_config.GEMINI_API_KEY == "abc-123"

    def test_invalid_key_raises(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "bad key with spaces")
        with pytest.raises(ValueError, match="Invalid GEMINI_API_KEY"):
            _ = runtime_config.GEMINI_API_KEY


class TestDerivedAndPassthroughAttrs:
    def test_gemini_model_default(self):
        assert runtime_config.GEMINI_MODEL == "gemini-pro"

    def test_gemini_model_custom(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MODEL", "gemini-1.5")
        assert runtime_config.GEMINI_MODEL == "gemini-1.5"

    def test_use_gemini_tracks_gemini_api_key_presence(self, monkeypatch):
        assert runtime_config.USE_GEMINI is False
        monkeypatch.setenv("GEMINI_API_KEY", "abc-123")
        assert runtime_config.USE_GEMINI is True

    def test_poolside_and_openrouter_keys_are_plain_passthrough(self, monkeypatch):
        assert runtime_config.POOLSIDE_API_KEY is None
        assert runtime_config.OPENROUTER_API_KEY is None
        monkeypatch.setenv("POOLSIDE_API_KEY", "pk-1")
        monkeypatch.setenv("OPENROUTER_API_KEY", "ok-1")
        assert runtime_config.POOLSIDE_API_KEY == "pk-1"
        assert runtime_config.OPENROUTER_API_KEY == "ok-1"

    def test_can_search_requires_both_google_keys(self, monkeypatch):
        assert runtime_config.CAN_SEARCH is False
        monkeypatch.setenv("GOOGLE_API_KEY", "g1")
        assert runtime_config.CAN_SEARCH is False
        monkeypatch.setenv("GOOGLE_CSE_ID", "c1")
        assert runtime_config.CAN_SEARCH is True

    def test_google_keys_read_at_access_time(self, monkeypatch):
        assert runtime_config.GOOGLE_API_KEY is None
        assert runtime_config.GOOGLE_CSE_ID is None
        monkeypatch.setenv("GOOGLE_API_KEY", "g1")
        monkeypatch.setenv("GOOGLE_CSE_ID", "c1")
        assert runtime_config.GOOGLE_API_KEY == "g1"
        assert runtime_config.GOOGLE_CSE_ID == "c1"


class TestTrustedUserIds:
    def test_unset_is_empty(self):
        assert runtime_config.TRUSTED_USER_IDS == []

    def test_parses_ids_and_skips_invalid_entries(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_USER_IDS", "111, 222,,not-a-number")
        assert runtime_config.TRUSTED_USER_IDS == [111, 222]


class TestPerBotSettings:
    """Inside an active bot's settings (grug#1667) each lazy attribute reads
    the bot's value first and falls back to the process environment."""

    def test_bot_settings_win_over_process_env(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_USER_IDS", "999")
        monkeypatch.setenv("OLLAMA_URLS", "http://process.example.com:11434")
        with bot_settings.activate({"TRUSTED_USER_IDS": "111", "GOOGLE_API_KEY": "bot-key"}):
            assert runtime_config.TRUSTED_USER_IDS == [111]
            assert runtime_config.GOOGLE_API_KEY == "bot-key"
            # Not set for the bot: the process value shows through.
            assert runtime_config.OLLAMA_URLS == ["http://process.example.com:11434"]
        assert runtime_config.TRUSTED_USER_IDS == [999]
        assert runtime_config.GOOGLE_API_KEY is None


def test_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError, match="has no attribute 'NOT_A_REAL_ATTR'"):
        _ = runtime_config.NOT_A_REAL_ATTR


def test_getattr_dispatch_covers_every_lazy_attr_name():
    """Every name __getattr__ used to hand-dispatch via if/elif must still
    resolve through the extracted-function lookup table (grug#632) - a typo
    in the new dict would silently turn a real attribute into an
    AttributeError with no test ever calling that exact name."""
    for name in (
        "OLLAMA_URLS", "OLLAMA_MODELS", "GEMINI_API_KEY", "GEMINI_MODEL",
        "USE_GEMINI", "POOLSIDE_API_KEY", "OPENROUTER_API_KEY", "CAN_SEARCH",
        "GOOGLE_API_KEY", "GOOGLE_CSE_ID", "TRUSTED_USER_IDS",
    ):
        getattr(runtime_config, name)  # must not raise
