# Config Module Tests

This directory contains comprehensive tests for the GrugThink configuration system.

## Overview

The tests are organized into two main files:

### 1. `test_manager.py` - Core Configuration Tests

Contains 14 tests covering the fundamental configuration validation that was previously handled by the old `config.py` module. These tests have been rewritten to work with the new `ConfigManager` class.

**Test Classes:**
- `TestConfigValidation` - Tests for configuration validation (7 tests)
  - `test_missing_discord_token` - Verifies handling of missing Discord token
  - `test_missing_llm_config` - Verifies handling of missing LLM configuration
  - `test_invalid_ollama_url` - Tests invalid Ollama URL handling
  - `test_invalid_ollama_model` - Tests invalid Ollama model handling
  - `test_invalid_gemini_api_key` - Tests invalid Gemini API key handling
  - `test_invalid_google_api_key` - Tests invalid Google API key handling
  - `test_invalid_google_cse_id` - Tests invalid Google CSE ID handling

- `TestValidConfig` - Tests for valid configuration scenarios (5 tests)
  - `test_valid_config_gemini` - Tests valid Gemini configuration
  - `test_valid_config_ollama` - Tests valid Ollama configuration
  - `test_trusted_user_ids` - Tests trusted user IDs configuration
  - `test_default_values` - Tests default configuration values
  - `test_log_level_case_insensitivity` - Tests case-insensitive log level

- `TestConfigLogging` - Tests for logging functionality (1 test)
  - `test_log_initial_settings` - Verifies initial settings are logged

- `TestPersonalitySave` - Tests for personality management (1 test)
  - `test_save_personality_to_file` - Tests saving personality to file

### 2. `test_advanced_features.py` - Advanced Configuration Tests

Contains 25 tests covering new features introduced with the ConfigManager system.

**Test Classes:**
- `TestYAMLLoading` - YAML file loading (3 tests)
  - `test_load_yaml_config` - Tests loading configuration from YAML
  - `test_create_default_yaml_if_not_exists` - Tests default YAML creation
  - `test_yaml_not_available_fallback` - Tests fallback when YAML unavailable

- `TestEnvironmentOverride` - Environment variable override (3 tests)
  - `test_env_var_overrides_config` - Tests env vars override config
  - `test_config_fallback_when_env_not_set` - Tests config fallback
  - `test_set_env_var_updates_config` - Tests setting env vars

- `TestConfigMigration` - Configuration migration (2 tests)
  - `test_migrate_from_json` - Tests JSON to YAML migration
  - `test_migrate_handles_missing_file` - Tests missing file handling

- `TestDefaultConfigGeneration` - Default config generation (3 tests)
  - `test_default_config_structure` - Tests default config structure
  - `test_default_api_keys_structure` - Tests API keys structure
  - `test_default_templates_loaded` - Tests template loading

- `TestConfigFileWatching` - File watching functionality (3 tests)
  - `test_file_watcher_initialization` - Tests watcher initialization
  - `test_config_reload_on_change` - Tests config reload on change
  - `test_change_callbacks_triggered` - Tests callback triggering

- `TestConfigImportExport` - Import/export functionality (2 tests)
  - `test_export_config` - Tests config export
  - `test_import_config` - Tests config import

- `TestDiscordTokenManagement` - Discord token management (3 tests)
  - `test_add_discord_token` - Tests adding Discord token
  - `test_remove_discord_token` - Tests removing Discord token
  - `test_get_available_discord_token` - Tests getting available token

- `TestPersonalityManagement` - Personality management (3 tests)
  - `test_add_personality` - Tests adding personality
  - `test_update_personality` - Tests updating personality
  - `test_remove_personality` - Tests removing personality

- `TestBotConfigManagement` - Bot configuration management (3 tests)
  - `test_add_bot_config` - Tests adding bot config
  - `test_update_bot_config` - Tests updating bot config
  - `test_remove_bot_config` - Tests removing bot config

## Test Results

**Total Tests:** 39 (14 core + 25 advanced)
**Status:** All tests passing ✅
**Coverage:** Comprehensive coverage of config module functionality

## Key Changes from Legacy Tests

The new tests differ from the old `test_config.py` (now `test_config_legacy.py`) in several important ways:

1. **No Import-Time Validation:** The new ConfigManager doesn't validate configuration at import time, but rather stores values and validates them when used.

2. **YAML-Based:** Tests use YAML configuration files instead of relying solely on environment variables.

3. **Comprehensive Feature Testing:** Includes tests for new features like hot-reloading, migration, import/export, and multi-bot configuration.

4. **Proper Mocking:** Uses proper mocking of file watchers and external dependencies.

5. **Isolated Tests:** Each test uses temporary directories and files to avoid side effects.

## Running the Tests

```bash
# Run all config tests
PYTHONPATH=. pytest tests/unit/config/ -v

# Run specific test file
PYTHONPATH=. pytest tests/unit/config/test_manager.py -v
PYTHONPATH=. pytest tests/unit/config/test_advanced_features.py -v

# Run with coverage (requires pytest-cov)
PYTHONPATH=. pytest tests/unit/config/ --cov=src/grugthink/config --cov-report=term-missing
```

## Fixtures

All tests use common fixtures defined in each test file:

- `temp_config_dir` - Creates a temporary directory for config files
- `temp_config_file` - Creates a temporary config file path
- `mock_watcher` - Mocks the file watcher to avoid filesystem operations

## Migration Notes

The old `test_config.py` has been archived as `test_config_legacy.py`. It tests the old `config.py` module which has been refactored into the `config/` package. The legacy tests are kept for reference but should not be run as part of the regular test suite.

## Future Improvements

Potential areas for additional testing:

1. Concurrent access to configuration
2. Performance testing for large configurations
3. Error recovery and resilience testing
4. Integration tests with actual Discord bots
5. Edge cases in YAML parsing
6. Security testing for sensitive data handling
