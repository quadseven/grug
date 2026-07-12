#!/usr/bin/env python3
"""
Configuration Manager

Main ConfigManager class that coordinates all configuration operations.
"""

import copy
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..grug_structured_logger import get_logger
from . import loader, migration, personalities, templates, tokens, watcher
from .models import ConfigTemplate

log = get_logger(__name__)


class ConfigManager:
    """Manages dynamic configuration with hot-reloading."""

    def __init__(self, config_file: str = "grugthink_config.yaml"):
        # Resolve config file path with environment overrides and sensible defaults
        env_path = (
            os.getenv("GRUGTHINK_CONFIG_PATH") or os.getenv("GRUGTHINK_CONFIG_FILE") or os.getenv("GRUGTHINK_CONFIG")
        )

        resolved = None
        if env_path:
            p = Path(env_path)
            if p.is_dir():
                p = p / "grugthink_config.yaml"
            resolved = str(p)
        else:
            # Prefer persistent /data path over ephemeral /app path
            candidates = [
                Path("/data/grugthink_config.yaml"),
                Path("/app/grugthink_config.yaml"),
                Path(config_file),
            ]
            for c in candidates:
                if c.exists():
                    resolved = str(c)
                    break
            if not resolved:
                resolved = config_file

        self.config_file = resolved
        self.config_data: Dict[str, Any] = {}
        self.env_vars: Dict[str, str] = {}
        self.change_callbacks: List[Callable] = []
        self._lock = threading.Lock()

        # File watcher for hot-reloading (if watchdog available)
        self.observer, self.handler = watcher.create_observer_and_handler(self)

        # Built-in templates
        self.templates = templates.create_default_templates()

        # Load initial configuration
        self._load_config()
        watcher.start_watching(self)

        log.info("ConfigManager initialized", extra={"config_file": config_file, "templates": len(self.templates)})

    def _load_config(self):
        """Load configuration from file."""
        config_data = loader.load_config(self.config_file)

        if config_data:
            # Load external personalities
            external_personalities = personalities.load_external_personalities()
            if external_personalities:
                inline_personalities = config_data.get("personalities", {})
                config_data["personalities"] = {**inline_personalities, **external_personalities}
                log.info("Loaded external personalities", extra={"count": len(external_personalities)})

            with self._lock:
                self.config_data = config_data
                self.env_vars = config_data.get("environment", {})

                log.info(
                    "Loaded environment section from config",
                    extra={
                        "env_vars_count": len(self.env_vars),
                        "env_vars_keys": sorted(self.env_vars.keys()),
                        "config_file": self.config_file,
                    },
                )
        else:
            # Create default configuration
            default_config = loader.create_default_config(self.config_file, self.templates)
            with self._lock:
                self.config_data = default_config
                self.env_vars = default_config.get("environment", {})

                log.info(
                    "Created default environment section",
                    extra={"env_vars_count": len(self.env_vars), "env_vars_keys": sorted(self.env_vars.keys())},
                )

    def _reload_config(self):
        """Reload configuration and notify callbacks."""
        old_config = self.config_data.copy()
        old_env = self.env_vars.copy()

        loader.reload_config(self, old_config, old_env)

    def _save_config(self):
        """Save current configuration to file."""
        loader.save_config(self.config_file, self.config_data)

    def add_change_callback(self, callback: Callable):
        """Add callback for configuration changes."""
        self.change_callbacks.append(callback)

    def get_config(self, key: str = None) -> Any:
        """Get configuration value."""
        with self._lock:
            if key is None:
                return self.config_data.copy()

            keys = key.split(".")
            data = self.config_data

            for k in keys:
                if isinstance(data, dict) and k in data:
                    data = data[k]
                else:
                    return None

            # Return a deep copy so callers can't mutate internal state outside the lock
            return copy.deepcopy(data)

    def set_config(self, key: str, value: Any):
        """Set configuration value."""
        with self._lock:
            keys = key.split(".")
            data = self.config_data

            # Navigate to parent
            for k in keys[:-1]:
                if k not in data:
                    data[k] = {}
                data = data[k]

            # Set value
            data[keys[-1]] = value

            # Snapshot the completed mutation for the write below
            snapshot = copy.deepcopy(self.config_data)

        # Save to file outside the lock so disk I/O doesn't block other config access
        loader.save_config(self.config_file, snapshot)

    def get_env_var(self, key: str, default: str = None) -> str:
        """Get environment variable with fallback to config."""
        # First check actual environment
        env_value = os.getenv(key)
        if env_value is not None:
            log.debug(
                f"get_env_var: Found {key} in os.environ",
                extra={"key": key, "value": env_value, "source": "os.environ"},
            )
            return env_value

        # Then check config environment section
        with self._lock:
            config_value = self.env_vars.get(key, default)
            log.debug(
                "get_env_var: Checking config env_vars",
                extra={
                    "key": key,
                    "found_in_env_vars": key in self.env_vars,
                    "env_vars_keys": sorted(self.env_vars.keys()),
                    "env_vars_size": len(self.env_vars),
                    "config_value": config_value,
                    "default": default,
                    "returning": config_value,
                },
            )
            return config_value

    def set_env_var(self, key: str, value: str):
        """Set environment variable in config (not actual env)."""
        with self._lock:
            self.env_vars[key] = value
            self.config_data["environment"] = self.env_vars
            self._save_config()

    def get_environment_config(self) -> Dict[str, str]:
        """Get all environment variables from config."""
        with self._lock:
            return self.env_vars.copy()

    def get_api_keys(self, service: str) -> Dict[str, Any]:
        """Get API keys for a service."""
        return self.get_config(f"api_keys.{service}") or {}

    def set_api_key(self, service: str, key_name: str, value: str):
        """Set API key for a service."""
        current_keys = self.get_api_keys(service)
        current_keys[key_name] = value
        self.set_config(f"api_keys.{service}", current_keys)

    # Discord token management
    def add_discord_token(self, name: str, token: str) -> str:
        """Add a Discord bot token."""
        return tokens.add_discord_token(self, name, token)

    def remove_discord_token(self, token_id: str) -> bool:
        """Remove a Discord bot token by ID."""
        return tokens.remove_discord_token(self, token_id)

    def get_discord_tokens(self) -> List[Dict[str, Any]]:
        """Get all Discord tokens."""
        return tokens.get_discord_tokens(self.config_data)

    def get_available_discord_token(self) -> Optional[str]:
        """Get an available Discord token."""
        return tokens.get_available_discord_token(self.config_data)

    def get_discord_token_by_id(self, token_id: str) -> Optional[str]:
        """Get a Discord token by its ID."""
        return tokens.get_discord_token_by_id(self.config_data, token_id)

    # Template management
    def get_template(self, template_id: str) -> Optional[ConfigTemplate]:
        """Get bot configuration template."""
        return templates.get_template(self.config_data, self.templates, template_id)

    def list_templates(self) -> Dict[str, ConfigTemplate]:
        """List all available templates."""
        return templates.list_templates(self.config_data, self.templates)

    def sync_personalities_to_templates(self):
        """Automatically create bot templates for personalities that don't have them."""
        templates.sync_personalities_to_templates(self)

    def create_bot_env(self, template_id: str, discord_token: str, **overrides) -> Dict[str, str]:
        """Create environment variables for a bot from template."""
        return templates.create_bot_env(self, template_id, discord_token, **overrides)

    # Personality management
    def get_personality(self, personality_id: str) -> Optional[Dict[str, Any]]:
        """Get personality configuration by ID."""
        return personalities.get_personality(self.config_data, personality_id)

    def list_personalities(self) -> Dict[str, Dict[str, Any]]:
        """List all available personalities."""
        return personalities.list_personalities(self.config_data)

    def add_personality(self, personality_id: str, personality_config: Dict[str, Any]) -> bool:
        """Add a new personality configuration."""
        return personalities.add_personality(self, personality_id, personality_config)

    def update_personality(self, personality_id: str, updates: Dict[str, Any]) -> bool:
        """Update a personality configuration."""
        return personalities.update_personality(self, personality_id, updates)

    def save_personality_to_file(self, personality_id: str, data: Dict[str, Any]) -> bool:
        """Persist personality YAML in the personalities directory."""
        return personalities.save_personality_to_file(personality_id, data)

    def remove_personality(self, personality_id: str) -> bool:
        """Remove a personality configuration."""
        return personalities.remove_personality(self, personality_id)

    # Bot configuration management
    def add_bot_config(self, bot_config: Dict[str, Any]) -> str:
        """Add a bot configuration to the YAML config."""
        bot_configs = self.get_config("bot_configs") or {}
        bot_id = bot_config["bot_id"]
        bot_configs[bot_id] = bot_config
        self.set_config("bot_configs", bot_configs)
        log.info("Added bot configuration", extra={"bot_id": bot_id})
        return bot_id

    def remove_bot_config(self, bot_id: str) -> bool:
        """Remove a bot configuration."""
        bot_configs = self.get_config("bot_configs") or {}
        if bot_id in bot_configs:
            del bot_configs[bot_id]
            self.set_config("bot_configs", bot_configs)
            log.info("Removed bot configuration", extra={"bot_id": bot_id})
            return True
        return False

    def update_bot_config(self, bot_id: str, updates: Dict[str, Any]) -> bool:
        """Update a bot configuration."""
        bot_configs = self.get_config("bot_configs") or {}
        if bot_id in bot_configs:
            bot_configs[bot_id].update(updates)
            self.set_config("bot_configs", bot_configs)
            log.info("Updated bot configuration", extra={"bot_id": bot_id})
            return True
        return False

    def get_bot_config(self, bot_id: str) -> Optional[Dict[str, Any]]:
        """Get a bot configuration by ID."""
        bot_configs = self.get_config("bot_configs") or {}
        return bot_configs.get(bot_id)

    def list_bot_configs(self) -> Dict[str, Dict[str, Any]]:
        """List all bot configurations."""
        return self.get_config("bot_configs") or {}

    # Import/Export
    def export_config(self, filename: str = None) -> str:
        """Export current configuration to file."""
        return loader.export_config(self.config_file, self.config_data, filename)

    def import_config(self, filename: str):
        """Import configuration from file."""
        return loader.import_config(self.config_file, filename, self)

    # Migration
    def migrate_from_json(self, json_file: str) -> Dict[str, str]:
        """Migrate bot configurations from JSON file to YAML config."""
        return migration.migrate_from_json(self, json_file)

    def stop(self):
        """Stop the configuration manager."""
        watcher.stop_watching(self)
        log.info("ConfigManager stopped")
