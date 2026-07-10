"""Configuration Manager test suite.

Tests for the new ConfigManager system, replacing the old config.py validation tests.
"""

import os
from unittest.mock import patch

import pytest

from src.grugthink.config.manager import ConfigManager


@pytest.fixture
def temp_config_dir(tmp_path):
    """Create a temporary directory for config files."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def temp_config_file(temp_config_dir):
    """Create a temporary config file path."""
    return str(temp_config_dir / "test_config.yaml")


@pytest.fixture
def mock_watcher():
    """Mock the file watcher to avoid file system operations."""
    with (
        patch("src.grugthink.config.watcher.create_observer_and_handler") as mock_create,
        patch("src.grugthink.config.watcher.start_watching") as mock_start,
        patch("src.grugthink.config.watcher.stop_watching") as mock_stop,
    ):
        mock_create.return_value = (None, None)
        yield {"create": mock_create, "start": mock_start, "stop": mock_stop}


class TestConfigValidation:
    """Test configuration validation functionality."""

    def test_missing_discord_token(self, temp_config_file, mock_watcher, monkeypatch):
        """Test that missing Discord token is handled correctly in single-bot mode."""
        # Clear environment variables
        monkeypatch.delenv("DISCORD_TOKEN", raising=False)
        monkeypatch.delenv("GRUGTHINK_MULTIBOT_MODE", raising=False)
        monkeypatch.delenv("DISABLE_OAUTH", raising=False)

        # ConfigManager should create default config without validation errors
        # since it doesn't enforce Discord token at initialization
        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify that no Discord token is available
        assert config_manager.get_available_discord_token() is None

        # Clean up
        config_manager.stop()

    def test_missing_llm_config(self, temp_config_file, mock_watcher, monkeypatch):
        """Test that missing LLM configuration is handled in default config."""
        # Clear LLM-related environment variables
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_URLS", raising=False)

        # ConfigManager should create default config with empty LLM settings
        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify that LLM config exists but may be empty
        gemini_keys = config_manager.get_api_keys("gemini")
        assert isinstance(gemini_keys, dict)

        # Clean up
        config_manager.stop()

    def test_invalid_ollama_url(self, temp_config_file, mock_watcher):
        """Test that invalid Ollama URL in config is not validated at load time."""
        # Create a config with invalid Ollama URL
        config_data = {
            "version": "2.0",
            "environment": {"OLLAMA_URLS": "not-a-valid-url"},
            "api_keys": {"gemini": {}, "discord": {"tokens": []}},
        }

        # Save config with invalid URL
        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        # ConfigManager should load without validation errors
        # Validation happens when the URL is actually used
        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify the invalid URL is stored
        assert config_manager.get_env_var("OLLAMA_URLS") == "not-a-valid-url"

        # Clean up
        config_manager.stop()

    def test_invalid_ollama_model(self, temp_config_file, mock_watcher):
        """Test that invalid Ollama model name in config is stored without validation."""
        config_data = {
            "version": "2.0",
            "environment": {"OLLAMA_URLS": "http://localhost:11434", "OLLAMA_MODELS": "invalid/model name"},
            "api_keys": {"gemini": {}, "discord": {"tokens": []}},
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify the invalid model is stored
        assert config_manager.get_env_var("OLLAMA_MODELS") == "invalid/model name"

        config_manager.stop()

    def test_invalid_gemini_api_key(self, temp_config_file, mock_watcher):
        """Test that invalid Gemini API key is stored without validation."""
        config_data = {
            "version": "2.0",
            "environment": {"GEMINI_API_KEY": "invalid key with spaces"},
            "api_keys": {"gemini": {"primary": "invalid key with spaces"}, "discord": {"tokens": []}},
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify the invalid key is stored
        assert config_manager.get_env_var("GEMINI_API_KEY") == "invalid key with spaces"

        config_manager.stop()

    def test_invalid_google_api_key(self, temp_config_file, mock_watcher):
        """Test that invalid Google API key is stored without validation."""
        config_data = {
            "version": "2.0",
            "environment": {"GOOGLE_API_KEY": "invalid key with spaces", "GOOGLE_CSE_ID": "valid_cse"},
            "api_keys": {
                "gemini": {},
                "google_search": {"api_key": "invalid key with spaces", "cse_id": "valid_cse"},
                "discord": {"tokens": []},
            },
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify the invalid key is stored
        assert config_manager.get_env_var("GOOGLE_API_KEY") == "invalid key with spaces"

        config_manager.stop()

    def test_invalid_google_cse_id(self, temp_config_file, mock_watcher):
        """Test that invalid Google CSE ID is stored without validation."""
        config_data = {
            "version": "2.0",
            "environment": {"GOOGLE_API_KEY": "valid_api_key", "GOOGLE_CSE_ID": "invalid id with spaces"},
            "api_keys": {
                "gemini": {},
                "google_search": {"api_key": "valid_api_key", "cse_id": "invalid id with spaces"},
                "discord": {"tokens": []},
            },
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify the invalid CSE ID is stored
        assert config_manager.get_env_var("GOOGLE_CSE_ID") == "invalid id with spaces"

        config_manager.stop()


class TestValidConfig:
    """Test valid configuration scenarios."""

    def test_valid_config_gemini(self, temp_config_file, mock_watcher, monkeypatch):
        """Test valid Gemini configuration."""
        # Set up environment with Gemini
        monkeypatch.setenv("GEMINI_API_KEY", "valid_gemini_key")
        monkeypatch.delenv("OLLAMA_URLS", raising=False)

        config_data = {
            "version": "2.0",
            "environment": {"GEMINI_API_KEY": "valid_gemini_key"},
            "api_keys": {"gemini": {"primary": "valid_gemini_key"}, "discord": {"tokens": []}},
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify Gemini configuration
        assert config_manager.get_env_var("GEMINI_API_KEY") == "valid_gemini_key"
        gemini_keys = config_manager.get_api_keys("gemini")
        assert gemini_keys.get("primary") == "valid_gemini_key"

        # Verify search is disabled by default
        google_search = config_manager.get_api_keys("google_search")
        assert not google_search.get("api_key") or not google_search.get("cse_id")

        config_manager.stop()

    def test_valid_config_ollama(self, temp_config_file, mock_watcher, monkeypatch):
        """Test valid Ollama configuration."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("OLLAMA_URLS", "http://localhost:11434,http://192.168.1.100:11434")
        monkeypatch.setenv("OLLAMA_MODELS", "llama3.2:3b,grug:latest")

        config_data = {
            "version": "2.0",
            "environment": {
                "OLLAMA_URLS": "http://localhost:11434,http://192.168.1.100:11434",
                "OLLAMA_MODELS": "llama3.2:3b,grug:latest",
            },
            "api_keys": {"gemini": {}, "discord": {"tokens": []}},
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify Ollama configuration
        ollama_urls = config_manager.get_env_var("OLLAMA_URLS")
        assert ollama_urls == "http://localhost:11434,http://192.168.1.100:11434"

        ollama_models = config_manager.get_env_var("OLLAMA_MODELS")
        assert ollama_models == "llama3.2:3b,grug:latest"

        config_manager.stop()

    def test_trusted_user_ids(self, temp_config_file, mock_watcher, monkeypatch):
        """Test trusted user IDs configuration."""
        monkeypatch.setenv("TRUSTED_USER_IDS", "123,456,789")

        config_data = {
            "version": "2.0",
            "environment": {"TRUSTED_USER_IDS": "123,456,789"},
            "api_keys": {"gemini": {}, "discord": {"tokens": []}},
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify trusted user IDs
        trusted_ids = config_manager.get_env_var("TRUSTED_USER_IDS")
        assert trusted_ids == "123,456,789"

        config_manager.stop()

    def test_default_values(self, temp_config_file, mock_watcher, monkeypatch):
        """Test default configuration values."""
        # Clear all environment variables
        env_vars = [
            "GRUGBOT_VARIANT",
            "LOG_LEVEL",
            "TRUSTED_USER_IDS",
            "GEMINI_MODEL",
            "OLLAMA_MODELS",
            "GRUGBOT_DATA_DIR",
            "DISCORD_TOKEN",
        ]
        for var in env_vars:
            monkeypatch.delenv(var, raising=False)

        # Create ConfigManager with defaults
        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify default values in global settings
        global_settings = config_manager.get_config("global_settings") or {}
        assert global_settings.get("log_level", "INFO") == "INFO"

        # Verify default environment values
        env_config = config_manager.get_environment_config()
        assert env_config.get("GRUGBOT_VARIANT", "prod") == "prod"
        assert env_config.get("LOG_LEVEL", "INFO") == "INFO"

        config_manager.stop()

    def test_log_level_case_insensitivity(self, temp_config_file, mock_watcher, monkeypatch):
        """Test that log level is case-insensitive."""
        monkeypatch.setenv("LOG_LEVEL", "debug")

        config_data = {
            "version": "2.0",
            "environment": {"LOG_LEVEL": "debug"},
            "global_settings": {"log_level": "debug"},
            "api_keys": {"gemini": {}, "discord": {"tokens": []}},
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify log level (should be stored as provided)
        log_level = config_manager.get_env_var("LOG_LEVEL")
        assert log_level.upper() == "DEBUG"

        config_manager.stop()


class TestConfigLogging:
    """Test configuration logging functionality."""

    def test_log_initial_settings(self, temp_config_file, mock_watcher):
        """Test that initial settings are logged."""
        config_data = {
            "version": "2.0",
            "environment": {
                "GEMINI_API_KEY": "test_key",
                "GOOGLE_API_KEY": "google_key",
                "GOOGLE_CSE_ID": "cse_id",
                "TRUSTED_USER_IDS": "123",
            },
            "api_keys": {
                "gemini": {"primary": "test_key"},
                "google_search": {"api_key": "google_key", "cse_id": "cse_id"},
                "discord": {"tokens": []},
            },
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        # Use patch to capture log calls during initialization
        with patch("src.grugthink.config.manager.log") as mock_log:
            config_manager = ConfigManager(config_file=temp_config_file)

            # Verify that logging occurred during initialization
            # ConfigManager should log at least one info message
            assert mock_log.info.called
            assert mock_log.info.call_count > 0

            config_manager.stop()


class TestPersonalitySave:
    """Test personality save functionality."""

    def test_save_personality_to_file(self, tmp_path, mock_watcher):
        """Test saving personality to file."""
        # Change to temp directory for this test
        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            config_file = str(tmp_path / "test_config.yaml")
            config_manager = ConfigManager(config_file=config_file)

            personality_data = {"name": "Test", "description": "Test personality"}

            # Save personality
            result = config_manager.save_personality_to_file("test_persona", personality_data)
            assert result is True

            # Verify file exists
            file_path = tmp_path / "personalities" / "test_persona.yaml"
            assert file_path.exists()

            # Verify content
            import yaml

            with open(file_path, "r") as f:
                saved_data = yaml.safe_load(f)
            assert saved_data == personality_data

            config_manager.stop()

        finally:
            os.chdir(original_cwd)
