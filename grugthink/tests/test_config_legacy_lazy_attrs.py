"""Tests for config_legacy.py's __getattr__ dispatch (grug#632).

Imports `config_legacy` directly rather than through `src.grugthink.config`
(the path `tests/test_config_legacy.py` uses) - that module resolves to an
unrelated config object (the multi-bot config MANAGER, not this file), which
is why every test in that file fails with AttributeError regardless of this
refactor; a pre-existing, separately-tracked breakage, out of scope for #632.

Each attribute is read fresh via __getattr__ on every access (no caching),
so these tests vary os.environ per-case against ONE module import rather
than reloading the module like test_config_legacy.py's (broken) suite does.
"""

import os

# config_legacy.py raises at IMPORT time if DISCORD_TOKEN is unset - same
# guard test_llm_fallback.py uses ahead of its own module-level import.
os.environ.setdefault("DISCORD_TOKEN", "fake_token")

import pytest  # noqa: E402

from src.grugthink import config_legacy  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in (
        "OLLAMA_URLS", "OLLAMA_MODELS", "GEMINI_API_KEY", "GEMINI_MODEL",
        "POOLSIDE_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CSE_ID",
    ):
        monkeypatch.delenv(var, raising=False)


class TestOllamaUrls:
    def test_default_is_empty_and_logs_a_warning(self, caplog):
        with caplog.at_level("WARNING"):
            assert config_legacy.OLLAMA_URLS == []
        assert "OLLAMA_URLS is empty" in caplog.text

    def test_parses_comma_separated_list(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_URLS", "http://localhost:11434, http://example.com:11434")
        assert config_legacy.OLLAMA_URLS == ["http://localhost:11434", "http://example.com:11434"]

    def test_invalid_url_raises(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_URLS", "not-a-valid-url")
        with pytest.raises(ValueError, match="Invalid OLLAMA_URL"):
            _ = config_legacy.OLLAMA_URLS


class TestOllamaModels:
    def test_default(self):
        assert config_legacy.OLLAMA_MODELS == ["llama3.2:3b"]

    def test_parses_comma_separated_list(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_MODELS", "llama3.2:3b,grug:latest")
        assert config_legacy.OLLAMA_MODELS == ["llama3.2:3b", "grug:latest"]

    def test_invalid_model_name_raises(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_MODELS", "invalid/model name")
        with pytest.raises(ValueError, match="Invalid model name"):
            _ = config_legacy.OLLAMA_MODELS


class TestGeminiApiKey:
    def test_unset_is_none(self):
        assert config_legacy.GEMINI_API_KEY is None

    def test_valid_key(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "abc-123")
        assert config_legacy.GEMINI_API_KEY == "abc-123"

    def test_invalid_key_raises(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "bad key with spaces")
        with pytest.raises(ValueError, match="Invalid GEMINI_API_KEY"):
            _ = config_legacy.GEMINI_API_KEY


class TestDerivedAndPassthroughAttrs:
    def test_gemini_model_default(self):
        assert config_legacy.GEMINI_MODEL == "gemini-pro"

    def test_gemini_model_custom(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MODEL", "gemini-1.5")
        assert config_legacy.GEMINI_MODEL == "gemini-1.5"

    def test_use_gemini_tracks_gemini_api_key_presence(self, monkeypatch):
        assert config_legacy.USE_GEMINI is False
        monkeypatch.setenv("GEMINI_API_KEY", "abc-123")
        assert config_legacy.USE_GEMINI is True

    def test_poolside_and_openrouter_keys_are_plain_passthrough(self, monkeypatch):
        assert config_legacy.POOLSIDE_API_KEY is None
        assert config_legacy.OPENROUTER_API_KEY is None
        monkeypatch.setenv("POOLSIDE_API_KEY", "pk-1")
        monkeypatch.setenv("OPENROUTER_API_KEY", "ok-1")
        assert config_legacy.POOLSIDE_API_KEY == "pk-1"
        assert config_legacy.OPENROUTER_API_KEY == "ok-1"

    def test_can_search_requires_both_google_keys(self, monkeypatch):
        assert config_legacy.CAN_SEARCH is False
        monkeypatch.setenv("GOOGLE_API_KEY", "g1")
        # CAN_SEARCH reads the module-level GOOGLE_API_KEY/GOOGLE_CSE_ID
        # constants (computed at import time), not freshly from os.environ -
        # unlike every other attribute here. Set both via monkeypatch.setattr
        # on the module itself to match that.
        monkeypatch.setattr(config_legacy, "GOOGLE_API_KEY", "g1")
        monkeypatch.setattr(config_legacy, "GOOGLE_CSE_ID", "c1")
        assert config_legacy.CAN_SEARCH is True


def test_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError, match="has no attribute 'NOT_A_REAL_ATTR'"):
        _ = config_legacy.NOT_A_REAL_ATTR


def test_getattr_dispatch_covers_every_lazy_attr_name():
    """Every name __getattr__ used to hand-dispatch via if/elif must still
    resolve through the extracted-function lookup table (grug#632) - a typo
    in the new dict would silently turn a real attribute into an
    AttributeError with no test ever calling that exact name."""
    for name in (
        "OLLAMA_URLS", "OLLAMA_MODELS", "GEMINI_API_KEY", "GEMINI_MODEL",
        "USE_GEMINI", "POOLSIDE_API_KEY", "OPENROUTER_API_KEY", "CAN_SEARCH",
    ):
        getattr(config_legacy, name)  # must not raise
