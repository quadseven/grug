"""FastAPI dependency injection functions."""

from typing import Any, Dict, List

from fastapi import Depends, HTTPException, Request

from ..bot_manager import BotManager
from ..config.manager import ConfigManager

# Global managers - will be set during app initialization
_bot_manager: BotManager = None
_config_manager: ConfigManager = None


def set_bot_manager(bot_manager: BotManager):
    """Set the global bot manager instance."""
    global _bot_manager
    _bot_manager = bot_manager


def set_config_manager(config_manager: ConfigManager):
    """Set the global config manager instance."""
    global _config_manager
    _config_manager = config_manager


def get_bot_manager() -> BotManager:
    """Dependency for getting the bot manager."""
    if _bot_manager is None:
        raise HTTPException(status_code=500, detail="Bot manager not initialized")
    return _bot_manager


def get_config_manager() -> ConfigManager:
    """Dependency for getting the config manager."""
    if _config_manager is None:
        raise HTTPException(status_code=500, detail="Config manager not initialized")
    return _config_manager


def _parse_id_list(config_manager: ConfigManager, key: str) -> List[str]:
    """Read a comma-separated ID list from config/env and normalize it."""
    raw = config_manager.get_env_var(key, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_current_user(request: Request, config_manager: ConfigManager = Depends(get_config_manager)) -> Dict[str, Any]:
    """Get current authenticated user from session."""
    # Check if OAuth is disabled
    disable_oauth = config_manager.get_env_var("DISABLE_OAUTH", "false").lower() == "true"

    if disable_oauth:
        # Return dummy user when OAuth is disabled
        return {"id": "admin", "username": "admin"}

    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def admin_required(
    user: Dict[str, Any] = Depends(get_current_user), config_manager: ConfigManager = Depends(get_config_manager)
) -> Dict[str, Any]:
    """Dependency to require admin permissions."""
    # Check if OAuth is disabled - if so, skip trusted user checks
    disable_oauth = config_manager.get_env_var("DISABLE_OAUTH", "false").lower() == "true"

    if disable_oauth:
        # When OAuth is disabled, allow all access
        return user

    # Get trusted users from config manager
    trusted = _parse_id_list(config_manager, "TRUSTED_USER_IDS")

    if user["id"] not in trusted:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def memory_manager_required(
    user: Dict[str, Any] = Depends(get_current_user), config_manager: ConfigManager = Depends(get_config_manager)
) -> Dict[str, Any]:
    """Check if user has memory management permissions."""
    # Check if OAuth is disabled - if so, allow all access
    disable_oauth = config_manager.get_env_var("DISABLE_OAUTH", "false").lower() == "true"

    if disable_oauth:
        return user

    # Get trusted admin users (they have full access)
    admin_users = _parse_id_list(config_manager, "TRUSTED_USER_IDS")

    # Check if user is admin (full access)
    if user["id"] in admin_users:
        return user

    # Get memory management users
    memory_users = _parse_id_list(config_manager, "TRUSTED_MEMORY_IDS")

    # Check if user has memory management access
    if user["id"] not in memory_users:
        raise HTTPException(status_code=403, detail="Memory management access required")

    return user
