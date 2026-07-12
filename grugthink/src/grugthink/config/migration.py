#!/usr/bin/env python3
"""
Configuration Migration

Handles migration from JSON bot configs to YAML configuration.
"""

import json
import os
import time
from typing import Any, Dict

from ..grug_structured_logger import get_logger
from .tokens import add_discord_token, get_discord_tokens

log = get_logger(__name__)


def migrate_from_json(config_manager, json_file: str) -> Dict[str, str]:
    """Migrate bot configurations from JSON file to YAML config.

    Returns a mapping of old bot IDs to Discord token IDs for reference.
    """
    if not os.path.exists(json_file):
        log.warning("JSON config file not found", extra={"file": json_file})
        return {}

    try:
        with open(json_file, "r") as f:
            json_configs = json.load(f)

        migration_map = {}
        migrated_configs = {}

        for json_config in json_configs:
            bot_id = json_config["bot_id"]
            discord_token = json_config["discord_token"]

            # Find matching token ID in YAML config
            token_id = None
            for token_data in get_discord_tokens(config_manager.config_data):
                if token_data["token"] == discord_token:
                    token_id = token_data["id"]
                    break

            if not token_id:
                # Token not found, add it
                token_name = f"Migrated - {json_config.get('name', bot_id)}"
                token_id = add_discord_token(config_manager, token_name, discord_token)

            # Create new bot config structure
            new_config = {
                "bot_id": bot_id,
                "name": json_config.get("name", f"Bot {bot_id}"),
                "discord_token_id": token_id,
                "template_id": _determine_template_from_json(json_config),
                "force_personality": json_config.get("force_personality"),
                "load_embedder": json_config.get("load_embedder", True),
                "log_level": json_config.get("log_level", "INFO"),
                "data_dir": json_config.get("data_dir", "./data"),
                "trusted_user_ids": json_config.get("trusted_user_ids"),
                "status": json_config.get("status", "stopped"),
                "created_at": json_config.get("created_at", time.time()),
                # Override fields for any custom API keys
                "override_gemini_key": json_config.get("gemini_api_key"),
                "override_google_api_key": json_config.get("google_api_key"),
                "override_google_cse_id": json_config.get("google_cse_id"),
                "override_ollama_urls": json_config.get("ollama_urls"),
                "override_ollama_models": json_config.get("ollama_models"),
            }

            # Remove None values to keep config clean
            new_config = {k: v for k, v in new_config.items() if v is not None}

            migrated_configs[bot_id] = new_config
            migration_map[bot_id] = token_id

            log.info(
                "Migrated bot config",
                extra={"bot_id": bot_id, "token_id": token_id, "template": new_config.get("template_id")},
            )

        # Save all migrated configs
        config_manager.set_config("bot_configs", migrated_configs)

        log.info("Migration completed", extra={"migrated_count": len(migrated_configs), "source_file": json_file})

        return migration_map

    except Exception as e:
        log.error("Migration failed", extra={"error": str(e), "file": json_file})
        raise


def _determine_template_from_json(json_config: Dict[str, Any]) -> str:
    """Determine the best template based on old JSON config."""
    force_personality = json_config.get("force_personality")
    load_embedder = json_config.get("load_embedder", True)
    has_ollama = json_config.get("ollama_urls") or json_config.get("ollama_models")

    if has_ollama:
        return "ollama_bot"
    elif force_personality == "grug":
        return "pure_grug" if load_embedder else "lightweight_grug"
    elif force_personality == "big_rob":
        return "pure_big_rob"
    elif force_personality is None:
        return "evolution_bot"
    else:
        return "evolution_bot"  # Default fallback
