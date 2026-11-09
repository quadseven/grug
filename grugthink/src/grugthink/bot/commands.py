"""Discord slash command handlers for GrugThink bot.

This module contains all Discord slash command implementations, extracted from
the main bot class for better organization and maintainability. Each command
is implemented as a standalone async function that accepts a bot instance.

All commands follow these conventions:
- Accept `bot` parameter as first argument for accessing bot state
- Use `interaction` for Discord interactions
- Include comprehensive Google-style docstrings per PEP 257
- Maintain type hints for all parameters
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from discord import app_commands

from .. import runtime_config as config
from ..bot.llm_clients import backend_ladder, last_served_backend
from ..logging_config import get_logger
from .commands_chat import (
    chat_frequency,
    force_chat,
    get_chat_settings,
    reset_activity,
    test_bot_chat,
    test_natural_chat,
)
from .commands_memory import grant_memory_access, list_memory_managers, revoke_memory_access
from .commands_user import (
    get_chat_frequency,
    help_command,
    learn,
    personality_info,
    verify,
    what_know,
)

if TYPE_CHECKING:
    from ..discord_bot import GrugThinkBot

log = get_logger(__name__)


# ============================================================================
# Command Metadata (names and descriptions for decorator application)
# ============================================================================

COMMAND_METADATA = {
    "verify": {"name": "verify", "description": "Verify the truthfulness of the previous message."},
    "learn": {"name": "learn", "description": "Teach the bot a new fact."},
    "get_chat_frequency": {
        "name": "get-chat-frequency",
        "description": "Check how often bot naturally chats in this channel",
    },
    "what_know": {"name": "what-know", "description": "See facts the bot knows (use web interface for full list)."},
    "help_command": {"name": "help", "description": "Shows what the bot can do."},
    "personality_info": {"name": "personality", "description": "Shows the bot's personality information."},
    "grant_memory_access": {
        "name": "grant-memory-access",
        "description": "Grant a user permission to modify bot's memory",
    },
    "revoke_memory_access": {
        "name": "revoke-memory-access",
        "description": "Remove a user's permission to modify memory",
    },
    "list_memory_managers": {"name": "list-memory-managers", "description": "List users who can modify bot memory"},
    "chat_frequency": {"name": "chat-frequency", "description": "Set how often the bot naturally chats (0-100%)"},
    "get_chat_settings": {
        "name": "get-chat-settings",
        "description": "View current chat frequency and conversation settings",
    },
    "reset_activity": {"name": "reset-activity", "description": "Reset channel activity tracking data"},
    "test_bot_chat": {"name": "test-bot-chat", "description": "[DEBUG] Test intelligent bot-to-bot conversation"},
    "test_natural_chat": {"name": "test-natural-chat", "description": "[DEBUG] Force test natural chat engagement"},
    "force_chat": {"name": "force-chat", "description": "[DEBUG] Force bot to chat immediately"},
    "ping": {"name": "ping", "description": "Test if the bot is responding"},
    "diagnose": {"name": "diagnose", "description": "[DEBUG] Diagnose bot configuration and setup"},
    "test_response": {"name": "test-response", "description": "[DEBUG] Test bot response without AI"},
    "repair_database": {"name": "repair-database", "description": "[DEBUG] Repair corrupted database"},
}


async def ping(interaction, bot: "GrugThinkBot") -> None:
    """Simple ping command to test bot responsiveness.

    Available to all users. Sends a "Pong!" response with the bot's name to
    confirm the bot is online and responding to commands.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends a pong response message.
    """
    bot_id = bot.get_bot_id()
    server_id = str(interaction.guild.id) if interaction.guild else "dm"
    personality = bot.personality_engine.get_personality(server_id)
    bot_name = personality.chosen_name or personality.name

    bot.log.info(
        "Ping command received",
        extra={"bot_id": bot_id, "user_id": interaction.user.id, "server_id": server_id, "bot_name": bot_name},
    )

    await interaction.response.send_message(f"🏓 Pong! I'm {bot_name} and I'm working!")


async def diagnose(interaction, bot: "GrugThinkBot") -> None:
    """Diagnose bot configuration and setup.

    Only users in TRUSTED_USER_IDS can use this command. Performs comprehensive
    diagnostics including API connectivity, database access, personality engine
    status, and chat frequency settings. Results are sent as a formatted message.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends diagnostic results as an ephemeral message.
    """
    # Check if user is a trusted admin
    if interaction.user.id not in config.TRUSTED_USER_IDS:
        await interaction.response.send_message("❌ You don't have permission to use debug commands.", ephemeral=True)
        return

    server_id = str(interaction.guild.id) if interaction.guild else "dm"

    # Check various configurations
    diagnostics = []

    # LLM backends, in the fallback order replies actually use. The list
    # comes from llm_clients.backend_ladder() - the same definition the
    # fallback chain walks - so this can never again claim the bot "uses"
    # one backend while another rung is configured or serving (#1693).
    diagnostics.append("LLM backends (fallback order):")
    for backend in backend_ladder():
        state = "configured" if backend.configured else "not configured"
        diagnostics.append(f"{'✅' if backend.configured else '❌'} {backend.name}: {state}")
    diagnostics.append(f"Ollama URLs: {', '.join(config.OLLAMA_URLS)}")
    last_served = last_served_backend(bot.get_bot_id())
    diagnostics.append(f"Last reply served by: {last_served if last_served else 'none yet'}")

    # Check bot configuration
    personality = bot.personality_engine.get_personality(server_id)
    bot_name = personality.chosen_name or personality.name
    diagnostics.append(f"✅ Bot Name: {bot_name}")
    diagnostics.append(f"✅ Personality Style: {personality.response_style}")

    # Check chat frequency
    chat_freq = bot.chat_frequencies.get(server_id, 0)
    diagnostics.append(f"✅ Chat Frequency: {chat_freq}%")

    # Check database
    try:
        server_db = bot.get_server_db(interaction.guild.id if interaction.guild else "dm")
        # Test database with a simple query
        facts_count = len(server_db.search_facts("test", k=1))
        db_path = getattr(server_db, "db_path", "unknown")
        diagnostics.append(f"✅ Database: Connected ({db_path})")
        diagnostics.append(f"✅ Database Facts: {facts_count} searchable")
    except Exception as e:
        diagnostics.append(f"❌ Database Error: {str(e)}")
        # Try to get more details about the database issue
        try:
            db_path = config.DB_PATH
            if os.path.exists(db_path):
                file_size = os.path.getsize(db_path)
                diagnostics.append(f"📁 Database file exists: {file_size} bytes")
            else:
                diagnostics.append("📁 Database file missing")
        except Exception as db_detail_error:
            diagnostics.append(f"📁 Cannot check database file: {str(db_detail_error)}")

    diagnostic_text = "\n".join(diagnostics)
    await interaction.response.send_message(f"🔍 **Bot Diagnostics**\n```\n{diagnostic_text}\n```", ephemeral=True)


async def test_response(interaction, bot: "GrugThinkBot") -> None:
    """Test bot mention and response without using AI.

    Only users in TRUSTED_USER_IDS can use this command. Generates a simple
    test response using the bot's name without invoking any AI models, useful
    for verifying basic bot mechanics are working.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends a test response message.
    """
    # Check if user is a trusted admin
    if interaction.user.id not in config.TRUSTED_USER_IDS:
        await interaction.response.send_message("❌ You don't have permission to use debug commands.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command only works in servers!", ephemeral=True)
        return

    server_id = str(interaction.guild.id)
    personality = bot.personality_engine.get_personality(server_id)
    bot_name = personality.chosen_name or personality.name

    # Create a simple test response
    test_response = f"Hello! I'm {bot_name} and I'm responding without AI. This proves the basic bot mechanics work!"

    await interaction.response.send_message(f"✅ **Test Response**\n{test_response}", ephemeral=True)


async def repair_database(interaction, bot: "GrugThinkBot") -> None:
    """Repair corrupted database by recreating it.

    Only users in TRUSTED_USER_IDS can use this command. Backs up the existing
    database file and creates a new empty database. This is a destructive
    operation that should only be used when the database is corrupted and
    cannot be accessed normally.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends repair status messages describing each step.
    """
    # Check if user is a trusted admin
    if interaction.user.id not in config.TRUSTED_USER_IDS:
        await interaction.response.send_message("❌ You don't have permission to use debug commands.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        server_id = str(interaction.guild.id) if interaction.guild else "dm"

        # Get the database path
        if bot.server_manager:
            server_db = bot.server_manager.get_server_db(server_id)
            db_path = server_db.db_path
        else:
            db_path = config.DB_PATH

        # Close existing connections
        if hasattr(bot, "server_manager") and bot.server_manager:
            if server_id in bot.server_manager.server_dbs:
                try:
                    old_db = bot.server_manager.server_dbs[server_id]
                    if hasattr(old_db, "conn") and old_db.conn:
                        old_db.conn.close()
                except Exception:
                    pass
                del bot.server_manager.server_dbs[server_id]

        # Backup and remove corrupted database
        backup_path = f"{db_path}.backup_{int(time.time())}"
        repair_log = []

        if os.path.exists(db_path):
            try:
                os.rename(db_path, backup_path)
                repair_log.append(f"✅ Backed up corrupted database to {backup_path}")
            except OSError as e:
                repair_log.append(f"⚠️ Could not backup database: {e}")
                try:
                    os.remove(db_path)
                    repair_log.append("✅ Removed corrupted database file")
                except OSError:
                    repair_log.append("❌ Could not remove corrupted database file")

        # Recreate database
        try:
            if bot.server_manager:
                new_db = bot.server_manager.get_server_db(server_id)
                repair_log.append("✅ Created new database")
                repair_log.append(f"✅ Database path: {new_db.db_path}")
            else:
                repair_log.append("❌ Server manager not available")
        except Exception as e:
            repair_log.append(f"❌ Failed to create new database: {e}")

        repair_text = "\n".join(repair_log)
        await interaction.followup.send(f"🔧 **Database Repair**\n```\n{repair_text}\n```", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Repair failed: {str(e)}", ephemeral=True)


# ============================================================================
# Command Registration
# ============================================================================


def register_commands(bot: "GrugThinkBot") -> None:
    """Register all slash commands with the bot instance.

    This function creates wrapper functions that remove the 'bot' parameter from
    command signatures, then applies @app_commands.command decorators programmatically.
    This avoids discord.py type annotation validation errors at module load time.

    Args:
        bot: The GrugThinkBot instance to register commands with.

    Returns:
        None.

    Example:
        >>> from grugthink.bot.commands import register_commands
        >>> bot = GrugThinkBot(client, bot_instance)
        >>> register_commands(bot)
    """
    import inspect

    # List all command functions (no decorators applied yet)
    command_functions = [
        verify,
        learn,
        get_chat_frequency,
        what_know,
        help_command,
        personality_info,
        grant_memory_access,
        revoke_memory_access,
        list_memory_managers,
        chat_frequency,
        get_chat_settings,
        reset_activity,
        test_bot_chat,
        test_natural_chat,
        force_chat,
        ping,
        diagnose,
        test_response,
        repair_database,
    ]

    # Create wrapper for each command and apply decorator
    for cmd_func in command_functions:
        # Get command metadata
        func_name = cmd_func.__name__
        if func_name not in COMMAND_METADATA:
            log.warning(f"No metadata found for command: {func_name}")
            continue

        metadata = COMMAND_METADATA[func_name]

        # Inspect function signature
        sig = inspect.signature(cmd_func)
        params = list(sig.parameters.values())
        # Original params: interaction, bot, [other params...]
        # We need to create wrapper with: interaction, [other params...]

        # Build wrapper function that captures bot in closure
        # CRITICAL: Must use proper closures, NOT default parameters
        # Discord.py inspects ALL parameters including defaults!

        def make_wrapper(cmd, bot_instance, params_list):
            """Factory to create wrapper with proper closures and typed parameters.

            The wrapper's __signature__ is (interaction, <the command's own
            parameters>), which is what discord.py inspects; the bot parameter
            stays hidden in the closure. The previous exec()-built wrapper got
            cmd and bot_instance as exec locals, which its body cannot see, so
            every command with parameters raised NameError when called.
            """
            # Skip interaction and bot parameters, get the rest
            cmd_params = params_list[2:]  # Skip interaction and bot

            if not cmd_params:
                # No extra parameters - simple case

                async def simple_wrapper(interaction):
                    await cmd(interaction, bot_instance)

                return simple_wrapper

            wrapper_params = [params_list[0].replace(annotation=inspect.Parameter.empty)]
            for p in cmd_params:
                annotation = p.annotation if p.annotation is not inspect.Parameter.empty else "str"
                default = p.default
                if default is not inspect.Parameter.empty and not isinstance(
                    default, (str, int, float, bool, type(None))
                ):
                    default = None
                wrapper_params.append(
                    p.replace(kind=inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=annotation, default=default)
                )

            signature = inspect.Signature(wrapper_params)
            interaction_name = wrapper_params[0].name

            async def bound_wrapper(*args, **kwargs):
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                arguments = dict(bound.arguments)
                interaction = arguments.pop(interaction_name)
                await cmd(interaction, bot_instance, **arguments)

            bound_wrapper.__signature__ = signature
            return bound_wrapper

        wrapper = make_wrapper(cmd_func, bot, params)

        # Copy over function metadata for better error messages
        wrapper.__name__ = func_name
        wrapper.__doc__ = cmd_func.__doc__

        # Apply @app_commands.command decorator programmatically
        # This creates a Command object, NOT a callable
        command_obj = app_commands.command(name=metadata["name"], description=metadata["description"])(wrapper)

        # Set the command directly on the bot
        setattr(bot, func_name, command_obj)

        # Add command to the tree explicitly (required for programmatic commands)
        bot.tree.add_command(command_obj)

    log.info(f"Registered {len(command_functions)} slash commands", extra={"bot_id": bot.get_bot_id()})
