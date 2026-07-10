#!/usr/bin/env python3
"""
Bot Manager Data Models

Defines BotConfig and BotInstance dataclasses for bot configuration and runtime state.
"""

import asyncio
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from discord.ext import commands

from ..grug_db import GrugServerManager
from ..personality_engine import PersonalityEngine


@dataclass
class BotConfig:
    """Configuration for a single bot instance."""

    bot_id: str
    name: str
    discord_token_id: str  # Reference to token ID in grugthink_config.yaml
    template_id: str = "evolution_bot"  # Template to use for this bot
    personality: Optional[str] = None  # Personality ID from personality configs
    force_personality: Optional[str] = None  # Deprecated, use personality instead
    load_embedder: bool = False  # Default to False to avoid memory issues in containers
    log_level: str = "DEBUG"
    data_dir: str = None  # Will be set from environment in __post_init__
    trusted_user_ids: Optional[str] = None
    enabled: bool = True  # Whether this bot should be enabled (configuration state)
    auto_start: Optional[bool] = None  # Whether to auto-start this bot on container startup
    created_at: float = None

    # Override settings (optional)
    override_gemini_key: Optional[str] = None
    override_google_api_key: Optional[str] = None
    override_google_cse_id: Optional[str] = None
    override_ollama_urls: Optional[str] = None
    override_ollama_models: Optional[str] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()
        if self.data_dir is None:
            # Use environment variable for data directory, fallback to ./data
            self.data_dir = os.getenv("GRUGBOT_DATA_DIR", "./data")


@dataclass
class BotInstance:
    """Runtime instance of a Discord bot."""

    config: BotConfig
    logger: Optional[Any] = None  # Add a logger field
    client: Optional[commands.Bot] = None
    personality_engine: Optional[PersonalityEngine] = None
    server_manager: Optional[GrugServerManager] = None
    bot_manager: Optional[Any] = None  # Reference to parent bot manager for cross-bot access
    thread: Optional[threading.Thread] = None
    task: Optional[asyncio.Task] = None
    last_heartbeat: float = None
    forced_personality: Optional[str] = None
    # Runtime status (memory only, not persisted)
    runtime_status: str = "stopped"  # stopped, starting, running, stopping, error
    # Health monitoring
    consecutive_failures: int = 0
    last_restart_attempt: float = None
    restart_count: int = 0
