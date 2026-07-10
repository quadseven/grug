#!/usr/bin/env python3
"""
Bot Lifecycle Management

Handles starting, stopping, and restarting bot instances.
"""

import asyncio
import os
import time

import discord
from discord.ext import commands

from ..grug_db import GrugServerManager
from ..grug_structured_logger import get_logger
from ..personality_engine import PersonalityEngine

log = get_logger(__name__)


async def start_bot(bot_manager, bot_id: str) -> bool:
    """Start a specific bot instance."""
    if bot_id not in bot_manager.bots:
        log.error("Bot not found", extra={"bot_id": bot_id})
        return False

    instance = bot_manager.bots[bot_id]
    config = instance.config

    # Check runtime status (not config status) to see if bot is actually running
    if instance.runtime_status == "running":
        log.warning("Bot already running", extra={"bot_id": bot_id})
        return True

    # Check if bot is disabled in configuration
    if not config.enabled:
        instance.logger.warning("Bot is disabled in configuration")
        return False

    try:
        instance.runtime_status = "starting"
        instance.logger.info("Starting bot", extra={"bot_name": config.name})

        # Create bot-specific environment and set it immediately
        bot_env = create_bot_environment(bot_manager, config)

        # DEBUG: Log what keys are in bot_env
        instance.logger.debug(
            "Bot environment created",
            extra={"bot_name": config.name, "env_keys": sorted(bot_env.keys()), "has_ollama": "OLLAMA_URLS" in bot_env},
        )

        # Set environment variables before importing any bot modules
        original_env = {}
        for key, value in bot_env.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value

        # Log initial settings for this bot instance
        instance.logger.info("Bot starting up", extra={"bot_name": config.name})
        if bot_env.get("GEMINI_API_KEY"):
            instance.logger.info(
                "Using Gemini for generation", extra={"model": bot_env.get("GEMINI_MODEL", "gemma-3-27b-it")}
            )
        elif bot_env.get("OLLAMA_URLS"):
            instance.logger.info(
                "Using Ollama for generation",
                extra={"urls": bot_env.get("OLLAMA_URLS"), "models": bot_env.get("OLLAMA_MODELS")},
            )
        if bot_env.get("GOOGLE_API_KEY"):
            instance.logger.info("Google Search is enabled.")
        else:
            instance.logger.warning("Google Search is disabled. Bot cannot learn new things from the internet.")
        if bot_env.get("TRUSTED_USER_IDS"):
            instance.logger.info("Trusted users configured", extra={"users": bot_env.get("TRUSTED_USER_IDS")})
        else:
            instance.logger.warning("No trusted users configured. /learn command will be disabled for all.")

        # Initialize bot components
        data_dir = os.path.join(config.data_dir, bot_id)
        os.makedirs(data_dir, exist_ok=True)

        # Initialize personality engine with configured personality (prefer new 'personality' field)
        bot_personality = getattr(config, "personality", None) or config.force_personality
        personality_engine = PersonalityEngine(
            db_path=os.path.join(data_dir, "personalities.db"), forced_personality=bot_personality
        )
        instance.personality_engine = personality_engine

        # Store the personality for this bot instance
        instance.forced_personality = bot_personality

        # Initialize server manager for this bot (each bot gets its own data directory)
        # Get load_embedder from environment (defaults to "true" if not set, converts to boolean)
        load_embedder_env = bot_env.get("LOAD_EMBEDDER", "true").lower() in ("true", "1", "yes")
        server_manager = GrugServerManager(
            base_db_path=os.path.join(data_dir, "facts.db"),
            model_name="all-MiniLM-L6-v2",
            load_embedder=load_embedder_env,
        )
        instance.server_manager = server_manager

        # Create Discord client
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True

        client = commands.Bot(command_prefix="/", intents=intents, loop=asyncio.get_running_loop())
        instance.client = client

        # Import and setup bot commands
        await setup_bot_commands(instance, bot_env)

        # Start the bot in a separate task - get token from environment
        discord_token = bot_env.get("DISCORD_TOKEN")
        if not discord_token:
            raise ValueError(f"No Discord token available for bot {config.bot_id}")
        instance.task = asyncio.create_task(client.start(discord_token))

        # Give it a moment to start
        await asyncio.sleep(2)

        # Check if the task failed
        if instance.task.done():
            exception = instance.task.exception()
            if exception:
                raise exception

        instance.runtime_status = "running"
        instance.last_heartbeat = time.time()
        # Reset failure count on successful start
        instance.consecutive_failures = 0

        instance.logger.info(
            "Bot started successfully", extra={"bot_name": config.name, "guild_count": len(client.guilds)}
        )

        return True

    except Exception as e:
        instance.runtime_status = "error"
        instance.consecutive_failures += 1
        instance.logger.error("Failed to start bot", extra={"error": str(e), "failures": instance.consecutive_failures})

        # Restore original environment variables on error
        try:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        except Exception:
            pass  # Don't let env cleanup crash the error handling

        return False


async def stop_bot(bot_manager, bot_id: str) -> bool:
    """Stop a specific bot instance."""
    if bot_id not in bot_manager.bots:
        return False

    instance = bot_manager.bots[bot_id]
    config = instance.config

    if instance.runtime_status != "running":
        return True

    try:
        instance.runtime_status = "stopping"
        instance.logger.info("Stopping bot", extra={"bot_name": config.name})

        # Close Discord client
        if instance.client:
            await instance.client.close()

        # Cancel the task
        if instance.task:
            instance.task.cancel()
            try:
                await instance.task
            except asyncio.CancelledError:
                pass

        # Clean up resources
        instance.client = None
        instance.task = None
        instance.last_heartbeat = None

        instance.runtime_status = "stopped"

        instance.logger.info("Bot stopped successfully")
        return True

    except Exception as e:
        instance.runtime_status = "error"
        instance.logger.error("Failed to stop bot", extra={"error": str(e)})
        return False


async def restart_bot(bot_manager, bot_id: str) -> bool:
    """Restart a specific bot instance."""
    if bot_id not in bot_manager.bots:
        log.error("Bot not found for restart", extra={"bot_id": bot_id})
        return False

    instance = bot_manager.bots[bot_id]
    instance.logger.info("Manual restart requested", extra={"bot_name": instance.config.name})

    # Import health module to use internal restart method
    from .health import attempt_bot_restart

    # Use the internal restart method which has proper backoff and error handling
    await attempt_bot_restart(bot_manager, bot_id, instance, "Manual restart requested")

    # Return success if bot is now running
    return instance.runtime_status == "running"


def create_bot_environment(bot_manager, config) -> dict:
    """Create environment variables for a specific bot."""
    discord_token = None

    # Try to get Discord token from ConfigManager first
    if bot_manager.config_manager and config.discord_token_id != "legacy":
        discord_token = bot_manager.config_manager.get_discord_token_by_id(config.discord_token_id)
        if not discord_token:
            raise ValueError(f"Discord token with ID '{config.discord_token_id}' not found")

    # If ConfigManager available, use it for environment creation
    if bot_manager.config_manager and discord_token:
        template_id = getattr(config, "template_id", "evolution_bot")

        env = bot_manager.config_manager.create_bot_env(
            template_id=template_id,
            discord_token=discord_token,
            LOG_LEVEL=config.log_level,
            GRUGBOT_DATA_DIR=os.path.join(config.data_dir, config.bot_id),
            LOAD_EMBEDDER=str(config.load_embedder),
        )

        # Apply bot-specific overrides
        if config.override_gemini_key:
            env["GEMINI_API_KEY"] = config.override_gemini_key
        if config.override_google_api_key:
            env["GOOGLE_API_KEY"] = config.override_google_api_key
        if config.override_google_cse_id:
            env["GOOGLE_CSE_ID"] = config.override_google_cse_id
        if config.override_ollama_urls:
            env["OLLAMA_URLS"] = config.override_ollama_urls
        if config.override_ollama_models:
            env["OLLAMA_MODELS"] = config.override_ollama_models
    else:
        # Legacy mode: create environment manually
        env = {}

        # For legacy tokens, check if bot instance has stored token
        bot_instance = bot_manager.bots.get(config.bot_id)
        if bot_instance and hasattr(bot_instance, "_legacy_discord_token"):
            discord_token = bot_instance._legacy_discord_token
        elif not discord_token:
            raise ValueError(f"No Discord token available for bot {config.bot_id}")

        env["DISCORD_TOKEN"] = discord_token
        env["GRUGBOT_DATA_DIR"] = os.path.join(config.data_dir, config.bot_id)
        env["LOG_LEVEL"] = config.log_level
        env["LOAD_EMBEDDER"] = str(config.load_embedder)

        # Legacy API key handling
        if config.override_gemini_key:
            env["GEMINI_API_KEY"] = config.override_gemini_key
        if config.override_google_api_key:
            env["GOOGLE_API_KEY"] = config.override_google_api_key
        if config.override_google_cse_id:
            env["GOOGLE_CSE_ID"] = config.override_google_cse_id
        if config.override_ollama_urls:
            env["OLLAMA_URLS"] = config.override_ollama_urls
        if config.override_ollama_models:
            env["OLLAMA_MODELS"] = config.override_ollama_models

    # Common configuration
    if config.trusted_user_ids:
        env["TRUSTED_USER_IDS"] = config.trusted_user_ids
    elif os.getenv("TRUSTED_USER_IDS"):
        env["TRUSTED_USER_IDS"] = os.getenv("TRUSTED_USER_IDS")

    # Personality configuration (prefer new 'personality' field over deprecated 'force_personality')
    personality = getattr(config, "personality", None) or config.force_personality
    if personality:
        env["FORCE_PERSONALITY"] = personality

    return env


async def setup_bot_commands(instance, env: dict):
    """Setup Discord commands for a bot instance."""
    client = instance.client

    @client.event
    async def on_ready():
        try:
            # Import GrugThinkBot from main package (not from ..bot which is a package)
            # The bot/ package shadows bot.py module, so we import from grugthink instead
            import traceback

            try:
                from grugthink import GrugThinkBot
            except Exception as import_error:
                log.error(
                    "GrugThinkBot import failed",
                    extra={"error": str(import_error), "traceback": traceback.format_exc()},
                )
                raise

            # Check if cog is already loaded to prevent duplicate loading
            cog_name = "GrugThinkBot"
            if client.get_cog(cog_name) is None:
                # Add the GrugThinkBot cog with proper instance
                await client.add_cog(GrugThinkBot(client, instance))
                instance.logger.info("GrugThinkBot cog added")
            else:
                instance.logger.debug("GrugThinkBot cog already loaded")

            # Mark as running BEFORE syncing commands so message handlers work even if sync fails
            instance.runtime_status = "running"
            instance.last_heartbeat = time.time()

            # Sync commands after adding cog (only if not already synced)
            # This can fail with interaction type errors but shouldn't break message handling
            try:
                await client.tree.sync()
                instance.logger.debug("Discord slash commands synced")
            except Exception as sync_error:
                instance.logger.warning(
                    "Failed to sync slash commands (message handling still works)", extra={"error": str(sync_error)}
                )

            instance.logger.info(
                "Bot connected to Discord",
                extra={
                    "bot_name": client.user.name,
                    "guild_count": len(client.guilds),
                },
            )
        except Exception as e:
            import traceback

            instance.logger.error(
                "Error in bot on_ready setup", extra={"error": str(e), "traceback": traceback.format_exc()}
            )
            instance.runtime_status = "error"
