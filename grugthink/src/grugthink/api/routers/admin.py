"""Admin settings API endpoints."""

import os
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException

from ...config.manager import ConfigManager
from ...logging_config import get_logger
from ..dependencies import admin_required, get_config_manager

router = APIRouter(prefix="/api/admin", tags=["admin"])
log = get_logger(__name__)


@router.get("/settings", dependencies=[Depends(admin_required)])
async def get_admin_settings(config_manager: ConfigManager = Depends(get_config_manager)):
    """Get all environment settings for admin panel."""
    # Get all environment variables from config
    env_vars = config_manager.get_environment_config()
    # Provide a stable shape with sensible defaults
    defaults = {
        "DISABLE_OAUTH": env_vars.get("DISABLE_OAUTH", "true"),
        "SESSION_SECRET": "***REDACTED***" if env_vars.get("SESSION_SECRET") else "",
        "TRUSTED_USER_IDS": env_vars.get("TRUSTED_USER_IDS", ""),
        "TRUSTED_MEMORY_IDS": env_vars.get("TRUSTED_MEMORY_IDS", ""),
        "LOG_LEVEL": env_vars.get("LOG_LEVEL", os.getenv("LOG_LEVEL", "INFO")),
        "GRUGBOT_VARIANT": env_vars.get("GRUGBOT_VARIANT", "dev"),
        "MULTIBOT_API_PORT": env_vars.get("MULTIBOT_API_PORT", os.getenv("API_PORT", "8080")),
        "GRUGBOT_DATA_DIR": env_vars.get("GRUGBOT_DATA_DIR", "/data"),
        "LOAD_EMBEDDER": env_vars.get("LOAD_EMBEDDER", "True"),
        "GEMINI_MODEL": env_vars.get("GEMINI_MODEL", "gemma-3-27b-it"),
        "OLLAMA_BASE_URL": env_vars.get("OLLAMA_BASE_URL", ""),
        "HEALTH_CHECK_INTERVAL": env_vars.get("HEALTH_CHECK_INTERVAL", "30"),
        "BOT_HEARTBEAT_TIMEOUT": env_vars.get("BOT_HEARTBEAT_TIMEOUT", "300"),
        "BOT_RESTART_RATE_LIMIT": env_vars.get("BOT_RESTART_RATE_LIMIT", "120"),
        "BOT_MAX_CONSECUTIVE_FAILURES": env_vars.get("BOT_MAX_CONSECUTIVE_FAILURES", "5"),
        "BOT_RESTART_BACKOFF_MAX": env_vars.get("BOT_RESTART_BACKOFF_MAX", "300"),
        "BOT_HIGH_LATENCY_THRESHOLD": env_vars.get("BOT_HIGH_LATENCY_THRESHOLD", "5.0"),
        "ENABLE_CONFIG_RELOAD": env_vars.get("ENABLE_CONFIG_RELOAD", "True"),
        "WEBSOCKET_ENABLED": env_vars.get("WEBSOCKET_ENABLED", "False"),
    }
    # Include any extra unknown keys (forward-compatibility)
    merged = {**env_vars, **defaults}
    return merged


@router.put("/settings", dependencies=[Depends(admin_required)])
async def update_admin_settings(settings: Dict[str, str], config_manager: ConfigManager = Depends(get_config_manager)):
    """Update environment settings."""
    try:
        # Update environment settings in config
        for key, value in settings.items():
            if value is not None:
                config_manager.set_env_var(key, str(value))

        log.info("Admin settings updated", extra={"settings_count": len(settings)})
        return {"message": "Settings updated successfully", "updated_count": len(settings)}

    except Exception as e:
        log.error("Failed to update admin settings", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to update settings: {str(e)}")
