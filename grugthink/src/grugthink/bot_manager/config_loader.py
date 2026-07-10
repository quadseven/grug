#!/usr/bin/env python3
"""
Bot Manager Configuration Loader

Handles loading and saving bot configurations from ConfigManager or legacy JSON.
"""

import json
import os
from dataclasses import asdict

from ..grug_structured_logger import get_logger
from .models import BotConfig, BotInstance

log = get_logger(__name__)


def load_configs(bot_manager, config_file: str = "bot_configs.json"):
    """Load bot configurations from ConfigManager or migrate from JSON."""
    if bot_manager.config_manager:
        # Try to load from YAML config first
        bot_configs = bot_manager.config_manager.list_bot_configs()

        if bot_configs:
            # Load from YAML
            for bot_id, config_data in bot_configs.items():
                try:
                    config = BotConfig(**config_data)
                    # Create a logger for the bot instance
                    bot_logger = get_logger(f"grugthink.bot.{config.bot_id}", bot_id=config.bot_id)
                    instance = BotInstance(config=config, bot_manager=bot_manager, logger=bot_logger)
                    bot_manager.bots[config.bot_id] = instance
                except Exception as e:
                    log.error("Failed to load bot config", extra={"bot_id": bot_id, "error": str(e)})

            log.info("Loaded bot configurations from YAML", extra={"count": len(bot_configs)})

        elif os.path.exists(config_file):
            # Migrate from JSON
            log.info("No bot configs in YAML, migrating from JSON", extra={"json_file": config_file})
            try:
                migration_map = bot_manager.config_manager.migrate_from_json(config_file)

                # Load the migrated configs
                bot_configs = bot_manager.config_manager.list_bot_configs()
                for bot_id, config_data in bot_configs.items():
                    config = BotConfig(**config_data)
                    bot_logger = get_logger(f"grugthink.bot.{bot_id}", bot_id=bot_id)
                    instance = BotInstance(config=config, bot_manager=bot_manager, logger=bot_logger)
                    bot_manager.bots[config.bot_id] = instance

                bot_manager._migrated = True
                log.info("Migration completed", extra={"migrated_count": len(migration_map), "json_file": config_file})

                # Optionally back up the old JSON file
                backup_file = config_file + ".migrated.backup"
                os.rename(config_file, backup_file)
                log.info("Backed up old JSON config", extra={"backup_file": backup_file})

            except Exception as e:
                log.error("Migration failed, falling back to JSON loading", extra={"error": str(e)})
                load_configs_from_json(bot_manager, config_file)
    else:
        # No ConfigManager, load from JSON (legacy mode)
        load_configs_from_json(bot_manager, config_file)


def load_configs_from_json(bot_manager, config_file: str):
    """Legacy method to load from JSON file."""
    if os.path.exists(config_file):
        try:
            with open(config_file, "r") as f:
                configs = json.load(f)

            for config_data in configs:
                # Convert old format to new format with dummy token_id
                if "discord_token" in config_data and "discord_token_id" not in config_data:
                    config_data["discord_token_id"] = "legacy"
                    config_data["template_id"] = "evolution_bot"
                    # Keep the actual token for legacy compatibility
                    config_data["_legacy_discord_token"] = config_data.pop("discord_token")

                config = BotConfig(**{k: v for k, v in config_data.items() if not k.startswith("_")})
                # Create a logger for the bot instance
                bot_logger = get_logger(f"grugthink.bot.{config.bot_id}", bot_id=config.bot_id)
                instance = BotInstance(config=config, bot_manager=bot_manager, logger=bot_logger)
                # Store legacy token if present
                if "_legacy_discord_token" in config_data:
                    instance._legacy_discord_token = config_data["_legacy_discord_token"]
                bot_manager.bots[config.bot_id] = instance

            log.info(
                "Loaded bot configurations from JSON",
                extra={"count": len(configs), "bot_ids": list(bot_manager.bots.keys())},
            )

        except Exception as e:
            log.error("Failed to load bot configurations", extra={"error": str(e), "config_file": config_file})


def save_configs(bot_manager):
    """Save current bot configurations to ConfigManager."""
    if not bot_manager.config_manager:
        log.warning("No ConfigManager available, cannot save bot configs")
        return

    try:
        for bot_instance in bot_manager.bots.values():
            config_dict = asdict(bot_instance.config)
            # Remove None values to keep config clean
            config_dict = {k: v for k, v in config_dict.items() if v is not None}

            # Update or add the bot config
            existing_config = bot_manager.config_manager.get_bot_config(bot_instance.config.bot_id)
            if existing_config:
                bot_manager.config_manager.update_bot_config(bot_instance.config.bot_id, config_dict)
            else:
                bot_manager.config_manager.add_bot_config(config_dict)

        log.info("Saved bot configurations to YAML", extra={"count": len(bot_manager.bots)})

    except Exception as e:
        log.error("Failed to save bot configurations", extra={"error": str(e)})


def reset_runtime_status(bot_manager):
    """Reset all bot runtime statuses to 'stopped' on startup.

    This fixes the dirty shutdown problem where bots remain marked as 'running'
    in configuration but are actually dead after container restart.
    """
    log.info("Resetting all bot runtime statuses on startup")

    for bot_id, instance in bot_manager.bots.items():
        old_status = instance.runtime_status
        instance.runtime_status = "stopped"
        instance.last_heartbeat = None
        instance.task = None
        instance.client = None
        # Reset health monitoring on startup
        instance.consecutive_failures = 0
        instance.last_restart_attempt = None
        instance.restart_count = 0

        log.debug(
            "Reset bot runtime status",
            extra={
                "bot_id": bot_id,
                "old_status": old_status,
                "new_status": "stopped",
                "enabled": instance.config.enabled,
            },
        )
