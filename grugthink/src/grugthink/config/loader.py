#!/usr/bin/env python3
"""
Configuration Loading and Saving

Handles loading and saving configuration files (YAML/JSON).
"""

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict

from ..grug_structured_logger import get_logger

log = get_logger(__name__)

# Optional YAML support
try:
    import yaml

    _YAML_AVAILABLE = True
except ImportError:
    yaml = None
    _YAML_AVAILABLE = False


def is_yaml_available() -> bool:
    """Check if YAML support is available."""
    return _YAML_AVAILABLE


def load_config(config_file: str) -> Dict[str, Any]:
    """Load configuration from file."""
    try:
        if os.path.exists(config_file):
            # Treat zero-byte or whitespace-only files as uninitialized
            try:
                if os.path.getsize(config_file) == 0:
                    raise ValueError("Config file is empty")
            except OSError:
                pass

            with open(config_file, "r") as f:
                if config_file.endswith(".yaml") or config_file.endswith(".yml"):
                    if _YAML_AVAILABLE:
                        data = yaml.safe_load(f)
                        if not data:
                            # Empty or invalid YAML
                            raise ValueError("Empty or invalid YAML config")
                    else:
                        log.warning("YAML config file found but PyYAML not available, skipping")
                        data = {}
                else:
                    data = json.load(f)

            log.info(
                "Configuration loaded", extra={"config_file": config_file, "env_vars": len(data.get("environment", {}))}
            )
            return data
        else:
            return {}

    except Exception as e:
        log.error("Failed to load configuration", extra={"error": str(e), "config_file": config_file})
        return {}


def save_config(config_file: str, config_data: Dict[str, Any]):
    """Save current configuration to file."""
    try:
        # Atomic write: write to temp then replace
        cfg_path = Path(config_file)
        tmp_path = cfg_path.with_suffix(cfg_path.suffix + ".tmp")

        with open(tmp_path, "w") as f:
            if config_file.endswith(".yaml") or config_file.endswith(".yml"):
                if _YAML_AVAILABLE:
                    yaml.safe_dump(config_data, f, indent=2, default_flow_style=False)
                else:
                    json.dump(config_data, f, indent=2)
            else:
                json.dump(config_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(str(tmp_path), str(cfg_path))

        try:
            size = os.path.getsize(config_file)
        except OSError:
            size = -1

        log.info("Configuration saved", extra={"config_file": config_file, "bytes": size})

    except Exception as e:
        log.error("Failed to save configuration", extra={"error": str(e)})
        raise


def create_default_config(config_file: str, templates: Dict) -> Dict[str, Any]:
    """Create default configuration file."""
    default_config = {
        "version": "2.0",
        "description": "GrugThink Multi-Bot Configuration",
        "global_settings": {
            "log_level": "INFO",
            "data_directory": "./data",
            "enable_monitoring": True,
            "api_rate_limit": 100,
        },
        "environment": {"GRUGBOT_VARIANT": "prod", "LOG_LEVEL": "INFO"},
        "api_keys": {
            "gemini": {"primary": "", "secondary": "", "fallback": ""},
            "google_search": {"api_key": "", "cse_id": ""},
            "discord": {"tokens": []},
        },
        "bot_templates": {template_id: asdict(template) for template_id, template in templates.items()},
    }

    try:
        with open(config_file, "w") as f:
            if config_file.endswith(".yaml") or config_file.endswith(".yml"):
                if _YAML_AVAILABLE:
                    yaml.safe_dump(default_config, f, indent=2, default_flow_style=False)
                else:
                    # Fall back to JSON if yaml not available
                    json.dump(default_config, f, indent=2)
            else:
                json.dump(default_config, f, indent=2)

        log.info("Created default configuration", extra={"config_file": config_file})
        return default_config

    except Exception as e:
        log.error("Failed to create default configuration", extra={"error": str(e)})
        raise


def reload_config(config_manager, old_config: Dict[str, Any], old_env: Dict[str, str]):
    """Reload configuration and notify callbacks."""
    config_data = load_config(config_manager.config_file)

    # Load external personalities
    from .personalities import load_external_personalities

    external_personalities = load_external_personalities()
    if external_personalities:
        inline_personalities = config_data.get("personalities", {})
        config_data["personalities"] = {**inline_personalities, **external_personalities}
        log.info("Loaded external personalities", extra={"count": len(external_personalities)})

    with config_manager._lock:
        config_manager.config_data = config_data
        config_manager.env_vars = config_data.get("environment", {})

    # Check for changes and notify callbacks
    if config_data != old_config or config_manager.env_vars != old_env:
        log.info(
            "Configuration changed, notifying callbacks", extra={"callbacks": len(config_manager.change_callbacks)}
        )

        for callback in config_manager.change_callbacks:
            try:
                callback(old_config, config_data, old_env, config_manager.env_vars)
            except Exception as e:
                log.error("Error in config change callback", extra={"error": str(e)})


def export_config(config_file: str, config_data: Dict[str, Any], filename: str = None) -> str:
    """Export current configuration to file."""
    if filename is None:
        timestamp = int(time.time())
        if _YAML_AVAILABLE:
            filename = f"grugthink_config_backup_{timestamp}.yaml"
        else:
            filename = f"grugthink_config_backup_{timestamp}.json"

    try:
        with open(filename, "w") as f:
            if filename.endswith(".yaml") or filename.endswith(".yml"):
                if _YAML_AVAILABLE:
                    yaml.safe_dump(config_data, f, indent=2, default_flow_style=False)
                else:
                    json.dump(config_data, f, indent=2)
            else:
                json.dump(config_data, f, indent=2)

        log.info("Configuration exported", extra={"filename": filename})
        return filename

    except Exception as e:
        log.error("Failed to export configuration", extra={"error": str(e), "filename": filename})
        raise


def import_config(config_file: str, filename: str, config_manager) -> Dict[str, Any]:
    """Import configuration from file."""
    try:
        with open(filename, "r") as f:
            if filename.endswith(".yaml") or filename.endswith(".yml"):
                if _YAML_AVAILABLE:
                    imported_config = yaml.safe_load(f)
                else:
                    raise ValueError("YAML config file provided but PyYAML not available")
            else:
                imported_config = json.load(f)

        with config_manager._lock:
            old_config = config_manager.config_data.copy()
            old_env = config_manager.env_vars.copy()

        # Persist before committing in-memory state, so a failed write doesn't
        # leave config_manager out of sync with what's on disk.
        save_config(config_file, imported_config)

        with config_manager._lock:
            config_manager.config_data = imported_config
            config_manager.env_vars = imported_config.get("environment", {})

        log.info("Configuration imported", extra={"filename": filename})

        # Notify callbacks with the actual pre-import state
        reload_config(config_manager, old_config, old_env)

        return imported_config

    except Exception as e:
        log.error("Failed to import configuration", extra={"error": str(e), "filename": filename})
        raise
