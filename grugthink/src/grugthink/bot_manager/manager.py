#!/usr/bin/env python3
"""
Bot Manager - Main Manager Class

Orchestrates multiple Discord bot instances with different personalities
and configurations within a single container.
"""

import asyncio
import threading
import uuid
from typing import Any, Dict, List, Optional

from ..grug_structured_logger import get_logger
from .config_loader import load_configs, reset_runtime_status, save_configs
from .health import monitor_bots
from .lifecycle import restart_bot, start_bot, stop_bot
from .models import BotConfig, BotInstance

log = get_logger(__name__)


class BotManager:
    """Manages multiple Discord bot instances."""

    def __init__(self, config_file: str = "bot_configs.json", config_manager=None):
        self.config_file = config_file  # Keep for backward compatibility during migration
        self.config_manager = config_manager
        self.bots: Dict[str, BotInstance] = {}
        self.running = False
        self._lock = threading.Lock()
        self._migrated = False

        # Load existing configurations
        load_configs(self, config_file)

        # Reset all runtime statuses on startup (fixes dirty shutdown issue)
        reset_runtime_status(self)

        log.info("BotManager initialized", extra={"config_file": config_file, "loaded_bots": len(self.bots)})

    def create_bot(self, name: str, discord_token_id: str, **kwargs) -> str:
        """Create a new bot configuration."""
        bot_id = str(uuid.uuid4())

        config = BotConfig(bot_id=bot_id, name=name, discord_token_id=discord_token_id, **kwargs)

        # Create a logger for the bot instance
        bot_logger = get_logger(f"grugthink.bot.{bot_id}", bot_id=bot_id)

        instance = BotInstance(config=config, bot_manager=self, logger=bot_logger)

        with self._lock:
            self.bots[bot_id] = instance
            save_configs(self)

        log.info(
            "Created new bot", extra={"bot_id": bot_id, "bot_name": name, "force_personality": config.force_personality}
        )

        return bot_id

    async def delete_bot(self, bot_id: str) -> bool:
        """Delete a bot configuration."""
        if bot_id not in self.bots:
            return False

        # Stop the bot if running - must await this!
        if self.bots[bot_id].runtime_status == "running":
            await stop_bot(self, bot_id)

        with self._lock:
            del self.bots[bot_id]
            # Remove from persistent configuration
            if self.config_manager:
                self.config_manager.remove_bot_config(bot_id)
            else:
                log.warning("No ConfigManager available, bot config may persist in file")

        log.info("Deleted bot", extra={"bot_id": bot_id})
        return True

    def update_bot_config(self, bot_id: str, **kwargs) -> bool:
        """Update bot configuration."""
        if bot_id not in self.bots:
            return False

        instance = self.bots[bot_id]
        config = instance.config

        # Update configuration
        for key, value in kwargs.items():
            if hasattr(config, key):
                setattr(config, key, value)

        with self._lock:
            save_configs(self)

        log.info("Updated bot configuration", extra={"bot_id": bot_id, "updated_fields": list(kwargs.keys())})

        return True

    def get_bot_status(self, bot_id: str) -> Optional[Dict[str, Any]]:
        """Get current status of a bot."""
        if bot_id not in self.bots:
            return None

        instance = self.bots[bot_id]
        config = instance.config

        # Get the actual personality being used (prefer new 'personality' field)
        actual_personality = getattr(config, "personality", None) or config.force_personality

        # If no explicit personality, try to get it from the template
        if not actual_personality and self.config_manager:
            template_id = getattr(config, "template_id", "evolution_bot")
            template = self.config_manager.get_template(template_id)
            if template:
                template_dict = template if isinstance(template, dict) else template.__dict__
                actual_personality = template_dict.get("personality")

        status = {
            "bot_id": bot_id,
            "name": config.name,
            "status": instance.runtime_status,  # Runtime status (not config status)
            "enabled": config.enabled,  # Configuration status
            "personality": actual_personality,  # Current personality
            "force_personality": config.force_personality,  # Deprecated but kept for compatibility
            "template_id": getattr(config, "template_id", "evolution_bot"),
            "discord_token_id": config.discord_token_id,
            "created_at": config.created_at,
            "last_heartbeat": instance.last_heartbeat,
            "guild_count": 0,
            "guild_ids": [],
            "log_level": getattr(config, "log_level", "INFO"),
            "load_embedder": getattr(config, "load_embedder", False),  # Default to False to avoid memory issues
            # Health monitoring info
            "consecutive_failures": instance.consecutive_failures,
            "restart_count": instance.restart_count,
            "last_restart_attempt": instance.last_restart_attempt,
        }

        # Add runtime info if bot is running
        if instance.client and instance.client.is_ready():
            status["guild_ids"] = [g.id for g in instance.client.guilds]
            status["guild_count"] = len(status["guild_ids"])
            status["latency"] = round(instance.client.latency * 1000, 2)  # ms

        return status

    def list_bots(self) -> List[Dict[str, Any]]:
        """List all bot configurations and their status."""
        return [self.get_bot_status(bot_id) for bot_id in self.bots.keys()]

    async def start_bot(self, bot_id: str) -> bool:
        """Start a specific bot instance."""
        return await start_bot(self, bot_id)

    async def stop_bot(self, bot_id: str) -> bool:
        """Stop a specific bot instance."""
        return await stop_bot(self, bot_id)

    async def restart_bot(self, bot_id: str) -> bool:
        """Restart a specific bot instance."""
        return await restart_bot(self, bot_id)

    async def start_all_bots(self):
        """Start all configured bots."""
        self.running = True

        for bot_id in self.bots.keys():
            await self.start_bot(bot_id)
            await asyncio.sleep(5)  # Stagger starts to avoid rate limits

    async def stop_all_bots(self):
        """Stop all running bots."""
        self.running = False

        tasks = []
        for bot_id in self.bots.keys():
            if self.bots[bot_id].runtime_status == "running":
                tasks.append(self.stop_bot(bot_id))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def monitor_bots(self):
        """Monitor bot health and update heartbeats with comprehensive health checking."""
        await monitor_bots(self)
