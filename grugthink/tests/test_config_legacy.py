"""Configuration validation test suite."""

import os
import sys
from unittest.mock import patch

import pytest


# Helper function to reload the config module
def _reload_config():
    # Remove the config module from cache to force reload
    modules_to_remove = [
        "src.grugthink.config",
        "src.grugthink",
    ]
    for module_name in modules_to_remove:
        if module_name in sys.modules:
            del sys.modules[module_name]

    from src.grugthink import config

    return config


@pytest.fixture(autouse=True)
def setup_config_env(monkeypatch):
    # Clear all relevant environment variables before each test
    env_vars = [
        "DISCORD_TOKEN",
        "GEMINI_API_KEY",
        "OLLAMA_URLS",
        "OLLAMA_MODELS",
        "GOOGLE_API_KEY",
        "GOOGLE_CSE_ID",
        "GRUGBOT_DATA_DIR",
        "GRUGBOT_VARIANT",
        "TRUSTED_USER_IDS",
        "LOG_LEVEL",
    ]
    for var in env_vars:
        monkeypatch.delenv(var, raising=False)

    # Set a default minimal valid configuration for tests that don't explicitly set them
    monkeypatch.setenv("DISCORD_TOKEN", "fake_token")
    monkeypatch.setenv("GEMINI_API_KEY", "fake_gemini_key")


def test_missing_discord_token(monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN")
    with pytest.raises(ValueError, match="Missing DISCORD_TOKEN"):
        _reload_config()


def test_missing_llm_config(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.delenv("OLLAMA_URLS", raising=False)
    with pytest.raises(ValueError, match="Missing LLM configuration"):
        _reload_config()


def test_invalid_ollama_url(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")  # Ensure Ollama is used
    monkeypatch.setenv("OLLAMA_URLS", "not-a-valid-url")
    with pytest.raises(ValueError, match="Invalid OLLAMA_URL"):
        _reload_config()


def test_invalid_ollama_model(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")  # Ensure Ollama is used
    monkeypatch.setenv("OLLAMA_URLS", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODELS", "invalid/model name")
    with pytest.raises(ValueError, match="Invalid model name"):
        _reload_config()


def test_invalid_gemini_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "invalid key with spaces")
    with pytest.raises(ValueError, match="Invalid GEMINI_API_KEY"):
        _reload_config()


def test_invalid_google_api_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "invalid key with spaces")
    monkeypatch.setenv("GOOGLE_CSE_ID", "fake_cse_id")  # Needs to be set for CAN_SEARCH to be true
    with pytest.raises(ValueError, match="Invalid GOOGLE_API_KEY"):
        _reload_config()


def test_invalid_google_cse_id(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake_api_key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "invalid id with spaces")
    with pytest.raises(ValueError, match="Invalid GOOGLE_CSE_ID"):
        _reload_config()


def test_valid_config_gemini(monkeypatch):
    monkeypatch.delenv("OLLAMA_URLS", raising=False)  # Ensure Ollama is not used
    config = _reload_config()
    assert config.USE_GEMINI is True
    assert config.CAN_SEARCH is False  # By default, no Google keys


def test_valid_config_ollama(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_URLS", "http://localhost:11434,http://192.168.1.100:11434")
    monkeypatch.setenv("OLLAMA_MODELS", "llama3.2:3b,grug:latest")
    config = _reload_config()
    assert config.USE_GEMINI is False
    assert config.OLLAMA_URLS == ["http://localhost:11434", "http://192.168.1.100:11434"]
    assert config.OLLAMA_MODELS == ["llama3.2:3b", "grug:latest"]


def test_trusted_user_ids(monkeypatch):
    monkeypatch.setenv("TRUSTED_USER_IDS", "123,456,789")
    config = _reload_config()
    assert config.TRUSTED_USER_IDS == [123, 456, 789]


def test_default_values(monkeypatch):
    # Ensure all relevant env vars are unset to get defaults
    monkeypatch.delenv("GRUGBOT_VARIANT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("TRUSTED_USER_IDS", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.delenv("GRUGBOT_DATA_DIR", raising=False)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)  # Ensure no token for default path
    # Set multibot mode to skip Discord token validation
    monkeypatch.setenv("GRUGTHINK_MULTIBOT_MODE", "true")

    config = _reload_config()
    assert config.GRUGBOT_VARIANT == "prod"
    assert config.LOG_LEVEL_STR == "INFO"
    assert config.TRUSTED_USER_IDS == []
    assert config.GEMINI_MODEL == "gemini-pro"
    assert config.OLLAMA_MODELS == ["llama3.2:3b"]  # Default if OLLAMA_MODELS not set but OLLAMA_URLS is
    # With no DISCORD_TOKEN, should use fallback path ending with "grug_lore.db"
    assert config.DB_PATH.endswith("grug_lore.db")
    # Check that DB_PATH is within the src/grugthink directory
    assert os.path.abspath(os.path.dirname(config.DB_PATH)) == os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../src/grugthink")
    )


def test_log_level_case_insensitivity(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    config = _reload_config()
    assert config.LOG_LEVEL_STR == "DEBUG"


@patch("logging.Logger.info")
def test_log_initial_settings(mock_log_info, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake_gemini_key")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake_google_key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "fake_cse_id")
    monkeypatch.setenv("TRUSTED_USER_IDS", "123")
    config = _reload_config()
    config.log_initial_settings()
    # Very basic check to see if logging is happening
    assert mock_log_info.call_count > 0


def test_save_personality_to_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.grugthink.config_manager import ConfigManager

    manager = ConfigManager(config_file="config.yaml")
    data = {"name": "Test", "description": "Test personality"}

    assert manager.save_personality_to_file("test_persona", data)
    file_path = tmp_path / "personalities" / "test_persona.yaml"
    assert file_path.exists()
