#!/usr/bin/env python3
"""
Personality Management

Handles personality configuration CRUD operations.
"""

import os
import re
from typing import Any, Dict, Optional

from ..grug_structured_logger import get_logger

log = get_logger(__name__)

# Personality ids become file names under personalities/<id>.yaml and arrive
# from the admin API, so a value like "../../etc/x" would escape the directory.
# Restrict ids to a conservative slug; this fullmatch is the sanitizer barrier
# that clears CodeQL py/path-injection at every personalities/ file sink.
_SAFE_PERSONALITY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PERSONALITIES_DIR = "personalities"


def is_safe_personality_id(personality_id: str) -> bool:
    """Return True if personality_id is a safe slug usable as a file name."""
    return bool(personality_id) and _SAFE_PERSONALITY_ID.fullmatch(personality_id) is not None


def personality_file_path(personality_id: str) -> str:
    """Return the on-disk YAML path for a personality.

    Raises ValueError if personality_id is not a safe slug, so no user-provided
    value can traverse outside the personalities/ directory.
    """
    if not is_safe_personality_id(personality_id):
        raise ValueError(f"unsafe personality id: {personality_id!r}")
    return os.path.join(_PERSONALITIES_DIR, f"{personality_id}.yaml")


# Optional YAML support
try:
    import yaml

    _YAML_AVAILABLE = True
except ImportError:
    yaml = None
    _YAML_AVAILABLE = False


def load_external_personalities() -> Dict[str, Any]:
    """Load personality configurations from personalities directory."""
    personalities = {}
    personalities_dir = "personalities"

    if not os.path.exists(personalities_dir):
        log.debug("Personalities directory not found", extra={"dir": personalities_dir})
        return personalities

    try:
        for filename in os.listdir(personalities_dir):
            if filename.endswith((".yaml", ".yml")):
                personality_id = filename.replace(".yaml", "").replace(".yml", "")
                file_path = os.path.join(personalities_dir, filename)

                try:
                    with open(file_path, "r") as f:
                        if _YAML_AVAILABLE:
                            personality_data = yaml.safe_load(f)
                            if personality_data:
                                personalities[personality_id] = personality_data
                                log.debug("Loaded personality", extra={"id": personality_id, "file": file_path})
                        else:
                            log.warning(
                                "YAML personality file found but PyYAML not available", extra={"file": file_path}
                            )
                except Exception as e:
                    log.error("Failed to load personality file", extra={"file": file_path, "error": str(e)})

    except Exception as e:
        log.error("Failed to scan personalities directory", extra={"error": str(e), "dir": personalities_dir})

    return personalities


def get_personality(config_data: Dict[str, Any], personality_id: str) -> Optional[Dict[str, Any]]:
    """Get personality configuration by ID."""
    personalities = config_data.get("personalities", {})
    return personalities.get(personality_id)


def list_personalities(config_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """List all available personalities."""
    return config_data.get("personalities", {})


def add_personality(config_manager, personality_id: str, personality_config: Dict[str, Any]) -> bool:
    """Add a new personality configuration."""
    personalities = config_manager.config_data.get("personalities", {})
    personalities[personality_id] = personality_config
    config_manager.set_config("personalities", personalities)
    save_personality_to_file(personality_id, personality_config)
    log.info("Added personality", extra={"personality_id": personality_id})
    return True


def update_personality(config_manager, personality_id: str, updates: Dict[str, Any]) -> bool:
    """Update a personality configuration."""
    personalities = config_manager.config_data.get("personalities", {})
    if personality_id in personalities:
        personalities[personality_id].update(updates)
        config_manager.set_config("personalities", personalities)
        save_personality_to_file(personality_id, personalities[personality_id])
        log.info("Updated personality", extra={"personality_id": personality_id})
        return True
    return False


def save_personality_to_file(personality_id: str, data: Dict[str, Any]) -> bool:
    """Persist personality YAML in the personalities directory."""
    if not _YAML_AVAILABLE:
        log.warning("PyYAML not available; cannot save personality file", extra={"personality_id": personality_id})
        return False

    # Inline allowlist guard on the tainted id immediately before the path sink,
    # so CodeQL sees the regex barrier dominating open() (py/path-injection).
    if not _SAFE_PERSONALITY_ID.fullmatch(personality_id or ""):
        log.error("Refusing to save personality with unsafe id", extra={"personality_id": personality_id})
        return False

    os.makedirs(_PERSONALITIES_DIR, exist_ok=True)
    file_path = os.path.join(_PERSONALITIES_DIR, f"{personality_id}.yaml")

    try:
        with open(file_path, "w") as f:
            yaml.safe_dump(data, f, indent=2, default_flow_style=False)
        log.debug("Saved personality file", extra={"personality_id": personality_id, "file": file_path})
        return True
    except Exception as e:
        log.error("Failed to save personality file", extra={"personality_id": personality_id, "error": str(e)})
        return False


def remove_personality(config_manager, personality_id: str) -> bool:
    """Remove a personality configuration."""
    personalities = config_manager.config_data.get("personalities", {})
    if personality_id in personalities:
        del personalities[personality_id]
        config_manager.set_config("personalities", personalities)

        # Also remove the physical file. Inline allowlist guard on the tainted id
        # right before the path sinks so CodeQL sees the regex barrier.
        if not _SAFE_PERSONALITY_ID.fullmatch(personality_id or ""):
            log.warning("Skipping file removal for unsafe personality id", extra={"personality_id": personality_id})
        else:
            file_path = os.path.join(_PERSONALITIES_DIR, f"{personality_id}.yaml")
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    log.debug("Removed personality file", extra={"personality_id": personality_id, "file": file_path})
            except Exception as e:
                log.warning(
                    "Failed to remove personality file", extra={"personality_id": personality_id, "error": str(e)}
                )

        log.info("Removed personality", extra={"personality_id": personality_id})
        return True
    return False
