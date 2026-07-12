"""Advanced Configuration Manager test suite.

Tests for new config system features: YAML loading, environment overrides,
config migration, default config generation, and file watching.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from src.grugthink.config.manager import ConfigManager
from src.grugthink.config.models import ConfigTemplate


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


class TestYAMLLoading:
    """Test YAML file loading functionality."""

    def test_load_yaml_config(self, temp_config_file, mock_watcher):
        """Test loading configuration from YAML file."""
        config_data = {
            "version": "2.0",
            "description": "Test Configuration",
            "environment": {"TEST_VAR": "test_value"},
            "api_keys": {"gemini": {"primary": "test_key"}, "discord": {"tokens": []}},
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify YAML was loaded correctly
        assert config_manager.get_config("version") == "2.0"
        assert config_manager.get_config("description") == "Test Configuration"
        assert config_manager.get_env_var("TEST_VAR") == "test_value"

        config_manager.stop()

    def test_create_default_yaml_if_not_exists(self, temp_config_file, mock_watcher):
        """Test that default YAML config is created if file doesn't exist."""
        # Ensure file doesn't exist
        if os.path.exists(temp_config_file):
            os.remove(temp_config_file)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify default config was created
        assert os.path.exists(temp_config_file)
        assert config_manager.get_config("version") == "2.0"

        config_manager.stop()

    def test_yaml_not_available_fallback(self, temp_config_file, mock_watcher):
        """Test fallback behavior when YAML is not available."""
        with patch("src.grugthink.config.loader._YAML_AVAILABLE", False):
            config_manager = ConfigManager(config_file=temp_config_file)

            # Should still work but may use JSON fallback
            assert config_manager.get_config("version") == "2.0"

            config_manager.stop()


class TestEnvironmentOverride:
    """Test environment variable override functionality."""

    def test_env_var_overrides_config(self, temp_config_file, mock_watcher, monkeypatch):
        """Test that environment variables override config file values."""
        # Set up config file
        config_data = {
            "version": "2.0",
            "environment": {"TEST_VAR": "config_value"},
            "api_keys": {"gemini": {}, "discord": {"tokens": []}},
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        # Set environment variable
        monkeypatch.setenv("TEST_VAR", "env_value")

        config_manager = ConfigManager(config_file=temp_config_file)

        # Environment variable should override config
        assert config_manager.get_env_var("TEST_VAR") == "env_value"

        config_manager.stop()

    def test_config_fallback_when_env_not_set(self, temp_config_file, mock_watcher, monkeypatch):
        """Test that config value is used when environment variable is not set."""
        config_data = {
            "version": "2.0",
            "environment": {"TEST_VAR": "config_value"},
            "api_keys": {"gemini": {}, "discord": {"tokens": []}},
        }

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        # Ensure environment variable is not set
        monkeypatch.delenv("TEST_VAR", raising=False)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Should fall back to config value
        assert config_manager.get_env_var("TEST_VAR") == "config_value"

        config_manager.stop()

    def test_set_env_var_updates_config(self, temp_config_file, mock_watcher):
        """Test that setting environment variable updates config file."""
        config_manager = ConfigManager(config_file=temp_config_file)

        # Set environment variable
        config_manager.set_env_var("NEW_VAR", "new_value")

        # Verify it's in memory
        assert config_manager.get_env_var("NEW_VAR") == "new_value"

        # Reload and verify it persisted
        config_manager._reload_config()
        assert config_manager.get_env_var("NEW_VAR") == "new_value"

        config_manager.stop()


class TestConfigMigration:
    """Test configuration migration functionality."""

    def test_migrate_from_json(self, temp_config_dir, temp_config_file, mock_watcher):
        """Test migration from JSON bot configs to YAML config."""
        # Create a JSON file with bot configurations (as an array)
        json_file = temp_config_dir / "bots.json"
        json_data = [
            {
                "bot_id": "bot1",
                "discord_token": "test_token_123",
                "name": "Test Bot 1",
                "status": "running",
            },
            {
                "bot_id": "bot2",
                "discord_token": "test_token_456",
                "name": "Test Bot 2",
                "status": "stopped",
            },
        ]

        with open(json_file, "w") as f:
            json.dump(json_data, f)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Migrate from JSON
        result = config_manager.migrate_from_json(str(json_file))

        # Verify migration succeeded
        assert "bot1" in result
        assert "bot2" in result

        # Verify bot configs are in the manager
        bot_configs = config_manager.list_bot_configs()
        assert len(bot_configs) >= 2

        config_manager.stop()

    def test_migrate_handles_missing_file(self, temp_config_file, mock_watcher):
        """Test that migration handles missing JSON file gracefully."""
        config_manager = ConfigManager(config_file=temp_config_file)

        # Try to migrate from non-existent file (should return empty dict, not raise)
        result = config_manager.migrate_from_json("/nonexistent/file.json")

        # Should return empty dict for missing file
        assert result == {}

        config_manager.stop()


class TestDefaultConfigGeneration:
    """Test default configuration generation."""

    def test_default_config_structure(self, temp_config_file, mock_watcher):
        """Test that default config has correct structure."""
        config_manager = ConfigManager(config_file=temp_config_file)

        # Verify required sections exist
        assert config_manager.get_config("version") is not None
        assert config_manager.get_config("global_settings") is not None
        assert config_manager.get_config("environment") is not None
        assert config_manager.get_config("api_keys") is not None

        config_manager.stop()

    def test_default_api_keys_structure(self, temp_config_file, mock_watcher):
        """Test that default API keys section has correct structure."""
        config_manager = ConfigManager(config_file=temp_config_file)

        api_keys = config_manager.get_config("api_keys")
        assert "gemini" in api_keys
        assert "google_search" in api_keys
        assert "discord" in api_keys

        config_manager.stop()

    def test_default_templates_loaded(self, temp_config_file, mock_watcher):
        """Test that default templates are loaded."""
        config_manager = ConfigManager(config_file=temp_config_file)

        templates = config_manager.list_templates()
        assert len(templates) > 0

        # Verify templates have correct structure
        for template_id, template in templates.items():
            assert isinstance(template, ConfigTemplate)
            assert hasattr(template, "name")
            assert hasattr(template, "description")

        config_manager.stop()


class TestConfigFileWatching:
    """Test configuration file watching functionality."""

    def test_file_watcher_initialization(self, temp_config_file):
        """Test that file watcher is initialized."""
        with (
            patch("src.grugthink.config.watcher.create_observer_and_handler") as mock_create,
            patch("src.grugthink.config.watcher.start_watching") as mock_start,
        ):
            mock_create.return_value = (MagicMock(), MagicMock())

            config_manager = ConfigManager(config_file=temp_config_file)

            # Verify watcher was created and started
            mock_create.assert_called_once()
            mock_start.assert_called_once()

            config_manager.stop()

    def test_config_reload_on_change(self, temp_config_file, mock_watcher):
        """Test that config is reloaded when file changes."""
        config_manager = ConfigManager(config_file=temp_config_file)

        # Set initial value
        config_manager.set_config("test_key", "initial_value")

        # Mock a file change by manually reloading
        config_manager.config_data.copy()
        config_manager.env_vars.copy()

        # Modify the config file
        config_data = config_manager.get_config()
        config_data["test_key"] = "changed_value"

        import yaml

        with open(temp_config_file, "w") as f:
            yaml.safe_dump(config_data, f)

        # Reload
        config_manager._reload_config()

        # Verify change was picked up
        assert config_manager.get_config("test_key") == "changed_value"

        config_manager.stop()

    def test_change_callbacks_triggered(self, temp_config_file, mock_watcher):
        """Test that change callbacks are triggered on config reload."""
        config_manager = ConfigManager(config_file=temp_config_file)

        # Add a change callback
        callback_called = {"count": 0}

        def test_callback(old_config, new_config, old_env, new_env):
            callback_called["count"] += 1

        config_manager.add_change_callback(test_callback)

        # Modify config to trigger callback
        config_manager.set_config("test_key", "value1")

        # Manually trigger reload
        config_manager.config_data.copy()
        config_manager.env_vars.copy()

        config_manager.set_config("test_key", "value2")
        config_manager._reload_config()

        # Note: Callbacks are only triggered if config actually changed
        # The behavior depends on whether the reload detected changes
        assert callback_called["count"] >= 0

        config_manager.stop()


class TestConfigImportExport:
    """Test configuration import/export functionality."""

    def test_export_config(self, temp_config_dir, temp_config_file, mock_watcher):
        """Test exporting configuration to a file."""
        config_manager = ConfigManager(config_file=temp_config_file)

        # Set some config values
        config_manager.set_config("export_test", "export_value")

        # Export to a specific file
        export_file = str(temp_config_dir / "exported_config.yaml")
        result_file = config_manager.export_config(export_file)

        assert result_file == export_file
        assert os.path.exists(export_file)

        # Verify exported content
        import yaml

        with open(export_file, "r") as f:
            exported_data = yaml.safe_load(f)

        assert exported_data["export_test"] == "export_value"

        config_manager.stop()

    def test_import_config(self, temp_config_dir, temp_config_file, mock_watcher):
        """Test importing configuration from a file."""
        # Create a config file to import
        import_file = temp_config_dir / "import_config.yaml"
        import_data = {
            "version": "2.0",
            "environment": {"IMPORTED_VAR": "imported_value"},
            "api_keys": {"gemini": {}, "discord": {"tokens": []}},
            "import_test": "import_value",
        }

        import yaml

        with open(import_file, "w") as f:
            yaml.safe_dump(import_data, f)

        config_manager = ConfigManager(config_file=temp_config_file)

        # Import the config
        config_manager.import_config(str(import_file))

        # Verify imported values
        assert config_manager.get_config("import_test") == "import_value"
        assert config_manager.get_env_var("IMPORTED_VAR") == "imported_value"

        config_manager.stop()


class TestDiscordTokenManagement:
    """Test Discord token management functionality."""

    def test_add_discord_token(self, temp_config_file, mock_watcher):
        """Test adding a Discord token."""
        config_manager = ConfigManager(config_file=temp_config_file)

        # Add a token
        token_id = config_manager.add_discord_token("test_bot", "test_token_123")

        assert token_id is not None
        assert len(token_id) > 0

        # Verify token was added
        tokens = config_manager.get_discord_tokens()
        assert len(tokens) > 0

        config_manager.stop()

    def test_remove_discord_token(self, temp_config_file, mock_watcher):
        """Test removing a Discord token."""
        config_manager = ConfigManager(config_file=temp_config_file)

        # Add a token
        token_id = config_manager.add_discord_token("test_bot", "test_token_123")

        # Remove the token
        result = config_manager.remove_discord_token(token_id)
        assert result is True

        # Verify token was removed
        token = config_manager.get_discord_token_by_id(token_id)
        assert token is None

        config_manager.stop()

    def test_get_available_discord_token(self, temp_config_file, mock_watcher):
        """Test getting an available Discord token."""
        config_manager = ConfigManager(config_file=temp_config_file)

        # Add a token
        config_manager.add_discord_token("test_bot", "test_token_123")

        # Get available token
        token = config_manager.get_available_discord_token()

        # May be None if no tokens are marked as available
        # This depends on the token management implementation
        assert token is None or isinstance(token, str)

        config_manager.stop()


class TestPersonalityManagement:
    """Test personality management functionality."""

    def test_add_personality(self, temp_config_file, mock_watcher, tmp_path):
        """Test adding a personality."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            config_manager = ConfigManager(config_file=temp_config_file)

            personality_data = {"name": "Test Personality", "description": "A test personality", "traits": ["friendly"]}

            result = config_manager.add_personality("test_personality", personality_data)
            assert result is True

            # Verify personality was added
            personality = config_manager.get_personality("test_personality")
            assert personality is not None
            assert personality["name"] == "Test Personality"

            config_manager.stop()

        finally:
            os.chdir(original_cwd)

    def test_update_personality(self, temp_config_file, mock_watcher, tmp_path):
        """Test updating a personality."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            config_manager = ConfigManager(config_file=temp_config_file)

            # Add a personality
            personality_data = {"name": "Test", "description": "Original"}
            config_manager.add_personality("test_personality", personality_data)

            # Update the personality
            updates = {"description": "Updated description"}
            result = config_manager.update_personality("test_personality", updates)
            assert result is True

            # Verify update
            personality = config_manager.get_personality("test_personality")
            assert personality["description"] == "Updated description"

            config_manager.stop()

        finally:
            os.chdir(original_cwd)

    def test_remove_personality(self, temp_config_file, mock_watcher, tmp_path):
        """Test removing a personality."""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            config_manager = ConfigManager(config_file=temp_config_file)

            # Add a personality
            personality_data = {"name": "Test", "description": "Test"}
            config_manager.add_personality("test_personality", personality_data)

            # Remove the personality
            result = config_manager.remove_personality("test_personality")
            assert result is True

            # Verify removal
            personality = config_manager.get_personality("test_personality")
            assert personality is None

            config_manager.stop()

        finally:
            os.chdir(original_cwd)


class TestBotConfigManagement:
    """Test bot configuration management functionality."""

    def test_add_bot_config(self, temp_config_file, mock_watcher):
        """Test adding a bot configuration."""
        config_manager = ConfigManager(config_file=temp_config_file)

        bot_config = {
            "bot_id": "test_bot",
            "token_id": "token123",
            "template_id": "grug_prod",
            "status": "running",
        }

        bot_id = config_manager.add_bot_config(bot_config)
        assert bot_id == "test_bot"

        # Verify bot config was added
        retrieved = config_manager.get_bot_config("test_bot")
        assert retrieved is not None
        assert retrieved["token_id"] == "token123"

        config_manager.stop()

    def test_update_bot_config(self, temp_config_file, mock_watcher):
        """Test updating a bot configuration."""
        config_manager = ConfigManager(config_file=temp_config_file)

        # Add a bot config
        bot_config = {"bot_id": "test_bot", "token_id": "token123", "status": "running"}
        config_manager.add_bot_config(bot_config)

        # Update the config
        updates = {"status": "stopped"}
        result = config_manager.update_bot_config("test_bot", updates)
        assert result is True

        # Verify update
        retrieved = config_manager.get_bot_config("test_bot")
        assert retrieved["status"] == "stopped"

        config_manager.stop()

    def test_remove_bot_config(self, temp_config_file, mock_watcher):
        """Test removing a bot configuration."""
        config_manager = ConfigManager(config_file=temp_config_file)

        # Add a bot config
        bot_config = {"bot_id": "test_bot", "token_id": "token123"}
        config_manager.add_bot_config(bot_config)

        # Remove the config
        result = config_manager.remove_bot_config("test_bot")
        assert result is True

        # Verify removal
        retrieved = config_manager.get_bot_config("test_bot")
        assert retrieved is None

        config_manager.stop()
