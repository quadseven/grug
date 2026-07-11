#!/usr/bin/env python3
"""
Discord Token Management

Handles Discord bot token CRUD operations.
"""

import time
import uuid
from typing import Any, Dict, List, Optional

from ..grug_structured_logger import get_logger

log = get_logger(__name__)


def add_discord_token(config_manager, name: str, token: str) -> str:
    """Add a Discord bot token."""
    tokens = config_manager.get_config("api_keys.discord.tokens") or []

    token_entry = {
        "id": str(uuid.uuid4()),
        "name": name,
        "token": token,
        "added_at": time.time(),
        "active": True,
    }

    tokens.append(token_entry)
    config_manager.set_config("api_keys.discord.tokens", tokens)

    log.info("Added Discord token", extra={"token_name": name, "token_id": token_entry["id"]})

    return token_entry["id"]


def remove_discord_token(config_manager, token_id: str) -> bool:
    """Remove a Discord bot token by ID."""
    tokens = config_manager.get_config("api_keys.discord.tokens") or []
    for idx, token in enumerate(tokens):
        if token["id"] == token_id:
            tokens.pop(idx)
            config_manager.set_config("api_keys.discord.tokens", tokens)
            log.info("Removed Discord token", extra={"token_id": token_id})
            return True
    return False


def get_discord_tokens(config_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Get all Discord tokens."""
    return config_data.get("api_keys", {}).get("discord", {}).get("tokens", [])


def get_available_discord_token(config_data: Dict[str, Any]) -> Optional[str]:
    """Get an available Discord token."""
    tokens = get_discord_tokens(config_data)
    for token_data in tokens:
        if token_data.get("active", True):
            return token_data["token"]
    return None


def get_discord_token_by_id(config_data: Dict[str, Any], token_id: str) -> Optional[str]:
    """Get a Discord token by its ID."""
    tokens = get_discord_tokens(config_data)
    for token_data in tokens:
        if token_data["id"] == token_id:
            return token_data["token"]
    return None
