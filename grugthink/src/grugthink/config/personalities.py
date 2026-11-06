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
# Personalities shipped in the image (read-only under readOnlyRootFilesystem).
_PERSONALITIES_DIR = "personalities"


def writable_personalities_dir() -> str:
    """Directory that API-created personality files are written to.

    GRUGTHINK_PERSONALITIES_DIR wins; otherwise <GRUGBOT_DATA_DIR>/personalities,
    which is the mounted data volume in the k8s deployment (the image's own
    personalities/ sits on a read-only root filesystem there). With neither set,
    as in local dev, it is the shipped personalities/ directory.
    """
    explicit = os.getenv("GRUGTHINK_PERSONALITIES_DIR")
    if explicit:
        return explicit
    data_dir = os.getenv("GRUGBOT_DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "personalities")
    return _PERSONALITIES_DIR


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
    return os.path.join(writable_personalities_dir(), f"{personality_id}.yaml")


# Optional YAML support
try:
    import yaml

    _YAML_AVAILABLE = True
except ImportError:
    yaml = None
    _YAML_AVAILABLE = False


def load_external_personalities() -> Dict[str, Any]:
    """Load personality files: the shipped directory, then the writable one.

    A file in the writable directory overrides a shipped one with the same id.
    """
    personalities: Dict[str, Any] = {}
    dirs = [_PERSONALITIES_DIR]
    writable = writable_personalities_dir()
    if os.path.abspath(writable) != os.path.abspath(_PERSONALITIES_DIR):
        dirs.append(writable)
    for personalities_dir in dirs:
        personalities.update(_load_personalities_from(personalities_dir))
    return personalities


def _load_personalities_from(personalities_dir: str) -> Dict[str, Any]:
    personalities: Dict[str, Any] = {}
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
    """Add a new personality configuration.

    The file is written first and the config saved only once it succeeds, so a
    failed write leaves the config untouched.
    """
    if not save_personality_to_file(personality_id, personality_config):
        log.error("Personality file write failed; config left unchanged", extra={"personality_id": personality_id})
        return False
    personalities = config_manager.get_config("personalities") or {}
    personalities[personality_id] = personality_config
    config_manager.set_config("personalities", personalities)
    log.info("Added personality", extra={"personality_id": personality_id})
    return True


def update_personality(config_manager, personality_id: str, updates: Dict[str, Any]) -> bool:
    """Update a personality configuration."""
    # get_config returns a deep copy, so nothing changes until set_config.
    personalities = config_manager.get_config("personalities") or {}
    if personality_id in personalities:
        updated = {**personalities[personality_id], **updates}
        if not save_personality_to_file(personality_id, updated):
            log.error("Personality file write failed; config left unchanged", extra={"personality_id": personality_id})
            return False
        personalities[personality_id] = updated
        config_manager.set_config("personalities", personalities)
        log.info("Updated personality", extra={"personality_id": personality_id})
        return True
    return False


def save_personality_to_file(personality_id: str, data: Dict[str, Any]) -> bool:
    """Persist personality YAML in the personalities directory."""
    if not _YAML_AVAILABLE:
        log.warning("PyYAML not available; cannot save personality file", extra={"personality_id": personality_id})
        return False

    # Guard the tainted id at the file sink (CodeQL py/path-injection): reject
    # non-slug ids, then confirm the RESOLVED path stays inside personalities/.
    if not _SAFE_PERSONALITY_ID.fullmatch(personality_id or ""):
        log.error("Refusing to save personality with unsafe id", extra={"personality_id": personality_id})
        return False

    target_dir = writable_personalities_dir()
    # basename strips any directory component, so the tainted id cannot traverse
    # out of personalities/ (CodeQL py/path-injection sanitizer).
    file_path = os.path.join(target_dir, os.path.basename(f"{personality_id}.yaml"))

    try:
        os.makedirs(target_dir, exist_ok=True)
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

        # Also remove the physical file. Guard the tainted id at the sink: reject
        # non-slug ids, then confirm the resolved path stays inside personalities/.
        if not _SAFE_PERSONALITY_ID.fullmatch(personality_id or ""):
            log.warning("Skipping file removal for unsafe personality id", extra={"personality_id": personality_id})
        else:
            # basename strips any directory component so the tainted id cannot
            # traverse out of personalities/ (CodeQL py/path-injection sanitizer).
            file_path = os.path.join(writable_personalities_dir(), os.path.basename(f"{personality_id}.yaml"))
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
