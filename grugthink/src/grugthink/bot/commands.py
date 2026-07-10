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

import asyncio
import os
import time
import traceback
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .. import config
from ..bot.prompts import is_rate_limited, query_model
from ..logging_config import get_logger

if TYPE_CHECKING:
    from ..bot import GrugThinkBot

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


# ============================================================================
# User-Facing Commands
# ============================================================================


async def verify(interaction, bot: "GrugThinkBot") -> None:
    """Verify the truthfulness of the last non-bot message in the channel.

    This command retrieves the most recent user message from the channel history
    and uses the AI model to verify its factual accuracy against the bot's knowledge
    base and personality context.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends verification result as a followup message to the interaction.

    Raises:
        No exceptions are raised; errors are handled and reported to the user.
    """
    if is_rate_limited(interaction.user.id, bot.get_bot_id()):
        await interaction.response.send_message("Slow down! Wait a few seconds.", ephemeral=True)
        return

    channel = interaction.channel
    history = [m async for m in channel.history(limit=25) if not m.author.bot and m.content.strip()]

    if not history:
        await interaction.response.send_message("No user message to verify.", ephemeral=True)
        return

    target = history[0].content
    log.info(
        "Verify command initiated",
        extra={"bot_id": bot.get_bot_id(), "user_id": str(interaction.user.id), "target_length": len(target)},
    )

    await interaction.response.defer(ephemeral=False)  # Tell Discord the bot is thinking

    # Get server ID and personality info
    server_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    personality = bot.personality_engine.get_personality(server_id)
    thinking_msg = f"{personality.chosen_name or personality.name} thinking..."

    msg = await interaction.followup.send(thinking_msg, ephemeral=False)

    try:
        # Get the server-specific database
        server_db = bot.get_server_db(interaction)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, query_model, target, server_db, server_id, bot.personality_engine, bot.get_bot_id()
        )

        if result:
            # Apply personality style to response
            styled_result = bot.personality_engine.get_response_with_style(server_id, result)
            await msg.edit(content=f"Verification: {styled_result}")
        else:
            error_msg = bot.personality_engine.get_error_message(server_id)
            await msg.edit(content=error_msg)

    except Exception as exc:
        log.error(
            "Slash command error",
            extra={"error": str(exc), "traceback": traceback.format_exc()},
        )
        # Use personality for error message
        error_msg = bot.personality_engine.get_error_message(server_id)
        await msg.edit(content=f"💥 {error_msg}")


async def learn(interaction, bot, fact: str) -> None:
    """Teach the bot a new fact to add to its knowledge base.

    Only trusted users (defined in TRUSTED_USER_IDS config) can teach the bot
    new facts. Facts must be at least 5 characters long. The response style
    adapts based on the server's personality configuration.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.
        fact: The factual statement to add to the bot's knowledge base.

    Returns:
        None. Sends confirmation or error message to the user.

    Raises:
        No exceptions are raised; errors are handled and reported to the user.
    """
    await interaction.response.defer(ephemeral=True)  # Tell Discord bot is thinking

    # Get personality info
    server_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    personality = bot.personality_engine.get_personality(server_id)
    bot_name = personality.chosen_name or personality.name

    if interaction.user.id not in config.TRUSTED_USER_IDS:
        if personality.response_style == "caveman":
            await interaction.followup.send(f"You not trusted to teach {bot_name}.", ephemeral=True)
        elif personality.response_style == "british_working_class":
            await interaction.followup.send("oi oi, you aint on the list mate, end of", ephemeral=True)
        else:
            await interaction.followup.send("You're not authorized to teach me facts.", ephemeral=True)
        return

    if len(fact.strip()) < 5:
        if personality.response_style == "caveman":
            await interaction.followup.send("Fact too short to be useful.", ephemeral=True)
        elif personality.response_style == "british_working_class":
            await interaction.followup.send("wot? thats it? need more than that mate, simple as", ephemeral=True)
        else:
            await interaction.followup.send("Please provide a more detailed fact.", ephemeral=True)
        return

    # Get the server-specific database
    server_db = bot.get_server_db(interaction)
    if server_db.add_fact(fact):
        log.info(
            "Fact learned",
            extra={
                "bot_id": bot.get_bot_id(),
                "user_id": str(interaction.user.id),
                "fact_length": len(fact),
                "server_id": str(interaction.guild_id),
            },
        )
        if personality.response_style == "caveman":
            await interaction.followup.send(f"{bot_name} learn: {fact}", ephemeral=True)
        elif personality.response_style == "british_working_class":
            await interaction.followup.send(f"sorted mate, learnt that: {fact}, nuff said", ephemeral=True)
        else:
            await interaction.followup.send(f"Learned: {fact}", ephemeral=True)
    else:
        if personality.response_style == "caveman":
            await interaction.followup.send(f"{bot_name} already know that.", ephemeral=True)
        elif personality.response_style == "british_working_class":
            await interaction.followup.send("already know that one, simple as", ephemeral=True)
        else:
            await interaction.followup.send("I already know that.", ephemeral=True)


async def get_chat_frequency(interaction, bot: "GrugThinkBot") -> None:
    """Display the bot's current natural chat frequency setting for the server.

    Shows the percentage value (0-100) indicating how frequently the bot will
    naturally engage in conversations without being directly mentioned.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends the current chat frequency as an ephemeral message.
    """
    server_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    frequency = bot.chat_frequencies.get(server_id, 0)  # Default to 0% if not set

    await interaction.response.send_message(
        f"My current chat frequency in this server is {frequency}%.", ephemeral=True
    )


async def what_know(interaction, bot, page: int = 1, search: str = None) -> None:
    """Display the bot's knowledge base with pagination and search capabilities.

    Shows up to 15 facts per page from the bot's knowledge base. Supports optional
    search filtering. Includes a link to the web interface for full management.
    Response style adapts to the server's personality configuration.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.
        page: Page number to display (default: 1, minimum: 1).
        search: Optional search term to filter facts.

    Returns:
        None. Sends an embedded message with the requested facts.
    """
    await interaction.response.defer(ephemeral=True)  # Tell Discord bot is thinking

    # Get personality info
    server_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    personality = bot.personality_engine.get_personality(server_id)
    bot_name = personality.chosen_name or personality.name

    # Get the server-specific database
    server_db = bot.get_server_db(interaction)

    # Handle search vs all facts
    if search:
        all_facts = server_db.search_facts(search, k=50)  # Search with higher limit
        search_mode = True
    else:
        all_facts = server_db.get_all_facts()
        search_mode = False

    if not all_facts:
        if search_mode:
            message = f"No facts found matching '{search}'"
        elif personality.response_style == "caveman":
            message = f"{bot_name} know nothing in this cave."
        elif personality.response_style == "british_working_class":
            message = "dont know nuffin yet mate, simple as"
        else:
            message = "I don't know any facts yet."

        await interaction.followup.send(message, ephemeral=True)
        return

    # Pagination setup
    FACTS_PER_PAGE = 15
    total_facts = len(all_facts)
    total_pages = max(1, (total_facts + FACTS_PER_PAGE - 1) // FACTS_PER_PAGE)

    # Validate page number
    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages

    # Calculate start and end indices for this page
    start_idx = (page - 1) * FACTS_PER_PAGE
    end_idx = min(start_idx + FACTS_PER_PAGE, total_facts)
    page_facts = all_facts[start_idx:end_idx]

    # Create a Discord embed for better formatting
    server_name = interaction.guild.name if interaction.guild else "DM"

    # Build title based on mode
    if search_mode:
        base_title = f"Search: '{search}'"
        total_desc = f"Found {total_facts} matching facts"
    elif personality.response_style == "caveman":
        base_title = f"{bot_name}'s Memories"
        total_desc = f"{bot_name} knows {total_facts} things in this cave"
    elif personality.response_style == "british_working_class":
        base_title = "wot i know"
        total_desc = f"got {total_facts} fings in me ed, nuff said"
    else:
        base_title = "Knowledge Base"
        total_desc = f"I know {total_facts} facts"

    title = f"{base_title} ({server_name}) - Page {page}/{total_pages}"
    description = f"{total_desc}\n🌐 **[View & Edit All Memories](http://localhost:8080)**"

    # Build fact list for this page
    MAX_FACT_LENGTH = 120  # Max length per individual fact
    fact_lines = []

    for i, fact in enumerate(page_facts, start=start_idx + 1):
        # Truncate long facts
        display_fact = fact[:MAX_FACT_LENGTH] + "..." if len(fact) > MAX_FACT_LENGTH else fact
        fact_lines.append(f"{i}. {display_fact}")

    # Create the fact list string
    fact_list = "\n".join(fact_lines)

    # Add navigation hints
    nav_hints = []
    if page > 1:
        nav_hints.append(f"Previous: `/what-know page:{page - 1}`")
    if page < total_pages:
        nav_hints.append(f"Next: `/what-know page:{page + 1}`")
    if search_mode:
        nav_hints.append("All facts: `/what-know`")
    else:
        nav_hints.append("Search: `/what-know search:term`")

    if nav_hints:
        fact_list += f"\n\n📝 {' | '.join(nav_hints)}"

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blue(),
    )

    # Use appropriate field name - for pagination we always show "Facts"
    embed.add_field(name="Facts", value=fact_list or "No facts to display", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


async def help_command(interaction, bot: "GrugThinkBot") -> None:
    """Display help information about available bot commands and features.

    Shows an embedded message listing all available commands with descriptions.
    The presentation style adapts to the server's personality configuration.
    Includes information about auto-verification through name mentions.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends an embedded help message.
    """
    # Get personality info
    server_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    personality = bot.personality_engine.get_personality(server_id)
    bot_name = personality.chosen_name or personality.name

    if personality.response_style == "caveman":
        title = f"{bot_name} Help"
        description = f"Here are the things {bot_name} can do:"
    elif personality.response_style == "british_working_class":
        title = "wot i can do"
        description = "right then, ere's wot im good for, simple as:"
    else:
        title = "Bot Help"
        description = "Here are my available commands:"

    embed = discord.Embed(title=title, description=description, color=discord.Color.green())
    embed.add_field(name="/verify", value="Verifies the truthfulness of the last message.", inline=False)
    embed.add_field(name="/learn", value="Teach me a new fact (trusted users only).", inline=False)
    embed.add_field(name="/what-know", value="See all the facts I know.", inline=False)
    embed.add_field(name="/personality", value="See my personality info and evolution.", inline=False)
    embed.add_field(name="/help", value="Shows this help message.", inline=False)

    # Add auto-verification feature description
    auto_verify_desc = f"Say '{bot_name}' or '@{bot_name}' in a message with a statement to auto-verify it!"
    if personality.response_style == "caveman":
        auto_verify_desc = f"Say '{bot_name}' with statement and {bot_name} check truth!"
    elif personality.response_style == "british_working_class":
        auto_verify_desc = "just say me name with summat and ill check it, simple as"

    embed.add_field(name="💬 Auto-Verification", value=auto_verify_desc, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


async def personality_info(interaction, bot: "GrugThinkBot") -> None:
    """Display the bot's personality information and evolution status.

    Shows an embedded message with the bot's name, evolution stage, interaction
    count, response style, and any developed personality quirks. Provides insight
    into how the bot's personality has developed over time in this server.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends an embedded personality information message.
    """
    # Get personality info
    server_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    personality_info = bot.personality_engine.get_personality_info(server_id)

    embed = discord.Embed(
        title=f"Personality: {personality_info['name']}",
        description="My personality and evolution status",
        color=discord.Color.purple(),
    )

    # Evolution stage descriptions
    stage_names = ["Initial", "Developing", "Established", "Evolved"]
    stage_name = stage_names[min(personality_info["evolution_stage"], 3)]

    embed.add_field(name="Name", value=personality_info["name"], inline=True)
    embed.add_field(name="Evolution Stage", value=f"{stage_name} ({personality_info['evolution_stage']})", inline=True)
    embed.add_field(name="Interactions", value=str(personality_info["interaction_count"]), inline=True)
    embed.add_field(name="Style", value=personality_info["style"], inline=True)

    if personality_info["quirks"]:
        quirks_text = ", ".join(personality_info["quirks"])
        embed.add_field(name="Developed Quirks", value=quirks_text, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================================
# Admin-Only Commands: Memory Management
# ============================================================================


async def grant_memory_access(interaction, bot, user) -> None:
    """Grant memory management permissions to a user.

    Only users in TRUSTED_USER_IDS can use this command. Adds the specified user
    to the TRUSTED_MEMORY_IDS list, allowing them to manage bot memories through
    the web interface.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.
        user: The Discord member to grant memory management access to.

    Returns:
        None. Sends confirmation or permission error message.
    """
    # Check if user is a full admin
    if interaction.user.id not in config.TRUSTED_USER_IDS:
        await interaction.response.send_message("❌ You don't have permission to manage memory access.", ephemeral=True)
        return

    # Get current memory managers from environment
    current_memory_ids = os.getenv("TRUSTED_MEMORY_IDS", "")
    memory_id_list = [uid.strip() for uid in current_memory_ids.split(",") if uid.strip()]

    user_id_str = str(user.id)
    if user_id_str in memory_id_list:
        await interaction.response.send_message(
            f"✅ {user.display_name} already has memory management access.", ephemeral=True
        )
        return

    # Add user to memory managers
    memory_id_list.append(user_id_str)
    new_memory_ids = ",".join(memory_id_list)

    # Update environment (this would need config manager in production)
    os.environ["TRUSTED_MEMORY_IDS"] = new_memory_ids

    log.info("Granted memory access", extra={"user_id": user_id_str, "granted_by": interaction.user.id})

    await interaction.response.send_message(
        f"✅ Granted memory management access to {user.display_name}\n"
        f"They can now manage bot memories through the web interface.",
        ephemeral=True,
    )


async def revoke_memory_access(interaction, bot, user) -> None:
    """Revoke memory management permissions from a user.

    Only users in TRUSTED_USER_IDS can use this command. Removes the specified
    user from the TRUSTED_MEMORY_IDS list, preventing them from managing bot
    memories through the web interface.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.
        user: The Discord member to revoke memory management access from.

    Returns:
        None. Sends confirmation or permission error message.
    """
    # Check if user is a full admin
    if interaction.user.id not in config.TRUSTED_USER_IDS:
        await interaction.response.send_message("❌ You don't have permission to manage memory access.", ephemeral=True)
        return

    # Get current memory managers from environment
    current_memory_ids = os.getenv("TRUSTED_MEMORY_IDS", "")
    memory_id_list = [uid.strip() for uid in current_memory_ids.split(",") if uid.strip()]

    user_id_str = str(user.id)
    if user_id_str not in memory_id_list:
        await interaction.response.send_message(
            f"✅ {user.display_name} doesn't have memory management access.", ephemeral=True
        )
        return

    # Remove user from memory managers
    memory_id_list.remove(user_id_str)
    new_memory_ids = ",".join(memory_id_list)

    # Update environment (this would need config manager in production)
    os.environ["TRUSTED_MEMORY_IDS"] = new_memory_ids

    log.info("Revoked memory access", extra={"user_id": user_id_str, "revoked_by": interaction.user.id})

    await interaction.response.send_message(
        f"✅ Revoked memory management access from {user.display_name}\nThey can no longer manage bot memories.",
        ephemeral=True,
    )


async def list_memory_managers(interaction, bot: "GrugThinkBot") -> None:
    """List all users with memory management permissions.

    Only users in TRUSTED_USER_IDS can use this command. Displays an embedded
    message showing all users currently authorized to manage bot memories through
    the web interface.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends an embedded message listing memory managers.
    """
    # Check if user is a full admin
    if interaction.user.id not in config.TRUSTED_USER_IDS:
        await interaction.response.send_message("❌ You don't have permission to view memory managers.", ephemeral=True)
        return

    # Get current memory managers
    current_memory_ids = os.getenv("TRUSTED_MEMORY_IDS", "")
    memory_id_list = [uid.strip() for uid in current_memory_ids.split(",") if uid.strip()]

    if not memory_id_list:
        await interaction.response.send_message("📝 No users currently have memory management access.", ephemeral=True)
        return

    # Try to get user objects for display
    managers = []
    for user_id in memory_id_list:
        try:
            user = await bot.client.fetch_user(int(user_id))
            managers.append(f"• {user.display_name} (`{user_id}`)")
        except Exception:
            managers.append(f"• Unknown User (`{user_id}`)")

    embed = discord.Embed(title="📝 Memory Managers", description="\n".join(managers), color=discord.Color.blue())
    embed.add_field(name="Total Managers", value=str(len(managers)), inline=True)
    embed.add_field(name="Web Interface", value="[Manage Memories](http://localhost:8080)", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================================
# Admin-Only Commands: Chat Configuration
# ============================================================================


async def chat_frequency(interaction, bot, percentage: int) -> None:
    """Set the bot's natural chat engagement frequency for the server.

    Only users in TRUSTED_USER_IDS can use this command. Controls how often the
    bot will naturally engage in conversations without being directly mentioned.
    Setting is persisted to disk and specific to each server.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.
        percentage: Chat frequency percentage (0-100). 0=never chat naturally,
            100=chat very frequently. Must be in range [0, 100].

    Returns:
        None. Sends confirmation message describing the new frequency level.
    """
    # Check if user is a trusted admin
    if interaction.user.id not in config.TRUSTED_USER_IDS:
        await interaction.response.send_message("❌ You don't have permission to modify chat settings.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command only works in servers!", ephemeral=True)
        return

    if percentage < 0 or percentage > 100:
        await interaction.response.send_message("Percentage must be between 0 and 100!", ephemeral=True)
        return

    server_id = str(interaction.guild.id)

    # Store the chat frequency setting
    bot.chat_frequencies[server_id] = percentage
    bot._save_chat_frequencies()  # Persist to disk

    # Get bot name for response
    personality = bot.personality_engine.get_personality(server_id)
    bot_name = personality.chosen_name or "Bot"

    if percentage == 0:
        response = f"{bot_name} will stay quiet unless directly mentioned."
    elif percentage <= 25:
        response = f"{bot_name} will occasionally join conversations ({percentage}% frequency)."
    elif percentage <= 50:
        response = f"{bot_name} will moderately participate in chat ({percentage}% frequency)."
    elif percentage <= 75:
        response = f"{bot_name} will actively participate in discussions ({percentage}% frequency)."
    else:
        response = f"{bot_name} will be very chatty and engage frequently ({percentage}% frequency)."

    bot.log.info(
        "Chat frequency updated",
        extra={
            "bot_id": bot.get_bot_id(),
            "server_id": server_id,
            "frequency": percentage,
            "user_id": str(interaction.user.id),
        },
    )

    await interaction.response.send_message(response, ephemeral=True)


async def get_chat_settings(interaction, bot: "GrugThinkBot") -> None:
    """View current chat frequency and conversation activity settings.

    Only users in TRUSTED_USER_IDS can use this command. Displays an embedded
    message showing the current chat frequency, recent activity statistics, and
    configured activity thresholds for natural conversation engagement.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends an embedded message with chat settings and activity data.
    """
    # Check if user is a trusted admin
    if interaction.user.id not in config.TRUSTED_USER_IDS:
        await interaction.response.send_message("❌ You don't have permission to view chat settings.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command only works in servers!", ephemeral=True)
        return

    server_id = str(interaction.guild.id)
    chat_frequency = bot.chat_frequencies.get(server_id, 0)

    # Get activity data if available
    activity_data = bot.channel_activity.get(str(interaction.channel.id), {})
    last_human = activity_data.get("last_human_message", 0)
    last_bot = activity_data.get("last_bot_message", 0)
    message_count = activity_data.get("message_count", 0)

    # Format timestamps
    human_ago = f"<t:{int(last_human)}:R>" if last_human else "Never"
    bot_ago = f"<t:{int(last_bot)}:R>" if last_bot else "Never"

    embed = discord.Embed(
        title="🎛️ Chat Settings", color=0x00FF00, description="Current conversation settings for this server"
    )

    embed.add_field(name="Chat Frequency", value=f"{chat_frequency}%", inline=True)
    embed.add_field(name="Message Count", value=str(message_count), inline=True)
    embed.add_field(name="Last Human Message", value=human_ago, inline=True)
    embed.add_field(name="Last Bot Message", value=bot_ago, inline=True)

    # Add threshold info
    embed.add_field(
        name="Activity Thresholds",
        value=(
            "• 5min human silence + 10min bot silence = conversation trigger\n"
            "• 3min bot silence for joining active conversations\n• 1-3% random engagement"
        ),
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


async def reset_activity(interaction, bot: "GrugThinkBot") -> None:
    """Reset channel activity tracking data for the current channel.

    Only users in TRUSTED_USER_IDS can use this command. Clears all stored
    activity data and conversation state for the channel, providing a fresh
    start for natural chat engagement tracking.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends confirmation message.
    """
    # Check if user is a trusted admin
    if interaction.user.id not in config.TRUSTED_USER_IDS:
        await interaction.response.send_message("❌ You don't have permission to reset activity data.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command only works in servers!", ephemeral=True)
        return

    channel_id = str(interaction.channel.id)

    # Reset activity data for this channel
    if channel_id in bot.channel_activity:
        del bot.channel_activity[channel_id]

    # Reset conversation states
    if channel_id in bot.conversation_states:
        del bot.conversation_states[channel_id]

    log.info(
        "Activity data reset",
        extra={
            "bot_id": bot.get_bot_id(),
            "channel_id": channel_id,
            "user_id": str(interaction.user.id),
        },
    )

    await interaction.response.send_message("✅ Channel activity data has been reset.", ephemeral=True)


# ============================================================================
# Debug Commands
# ============================================================================


async def test_bot_chat(interaction, bot: "GrugThinkBot") -> None:
    """Debug command to test intelligent bot conversation system.

    Only users in TRUSTED_USER_IDS can use this command. Creates a fake message
    to trigger the bot-to-bot conversation system for testing purposes.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends confirmation or error message.
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

    # Create a fake message to trigger the conversation system
    fake_message = type(
        "FakeMessage",
        (),
        {
            "author": interaction.user,
            "content": "Testing bot conversation",
            "channel": interaction.channel,
            "guild": interaction.guild,
        },
    )()

    # Trigger intelligent bot conversation
    try:
        await bot.initiate_bot_conversation(fake_message, server_id, personality, bot_name, "debug_test")
        await interaction.response.send_message("🤖 Bot conversation test triggered!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)


async def test_natural_chat(interaction, bot: "GrugThinkBot") -> None:
    """Force test natural chat engagement system.

    Only users in TRUSTED_USER_IDS can use this command. Creates mock messages
    and triggers the natural chat response system to test conversation engagement
    functionality.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends confirmation or error message.
    """
    # Check if user is a trusted admin
    if interaction.user.id not in config.TRUSTED_USER_IDS:
        await interaction.response.send_message("❌ You don't have permission to use debug commands.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command only works in servers!", ephemeral=True)
        return

    try:
        server_id = str(interaction.guild.id)
        personality = bot.personality_engine.get_personality(server_id)

        # Get current settings
        chat_frequency = bot.chat_frequencies.get(server_id, 0)

        # Create a mock message to trigger natural chat
        fake_message = type(
            "FakeMessage",
            (),
            {
                "content": "This is a test to trigger natural chat",
                "author": interaction.user,
                "channel": interaction.channel,
                "guild": interaction.guild,
                "id": 12345,
            },
        )()

        # Add some mock recent messages for context
        if server_id not in bot.last_messages:
            bot.last_messages[server_id] = []

        # Clear old messages and add test messages
        bot.last_messages[server_id] = [
            {
                "author": interaction.user.display_name or interaction.user.name,
                "content": "Hello there!",
                "timestamp": time.time() - 60,  # 1 minute ago
                "is_bot": False,
            },
            {
                "author": interaction.user.display_name or interaction.user.name,
                "content": "How are things going?",
                "timestamp": time.time() - 30,  # 30 seconds ago
                "is_bot": False,
            },
        ]

        # Respond to interaction first to avoid timeout
        await interaction.response.send_message(
            f"🤖 Natural chat test triggered! Chat frequency: {chat_frequency}%", ephemeral=True
        )

        # Force natural chat engagement by bypassing normal checks
        await bot.generate_natural_response(fake_message, server_id, personality, bot.last_messages[server_id])
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)


async def force_chat(interaction, bot: "GrugThinkBot") -> None:
    """Force bot to chat immediately, bypassing all checks.

    Only users in TRUSTED_USER_IDS can use this command. Makes the bot send a
    simple test message to the channel immediately, useful for testing basic
    functionality.

    Args:
        bot: The GrugThinkBot instance containing bot state and configuration.
        interaction: The Discord interaction object from the slash command.

    Returns:
        None. Sends confirmation or error message.
    """
    # Check if user is a trusted admin
    if interaction.user.id not in config.TRUSTED_USER_IDS:
        await interaction.response.send_message("❌ You don't have permission to use debug commands.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command only works in servers!", ephemeral=True)
        return

    try:
        await interaction.response.send_message("🤖 Forcing bot to chat...", ephemeral=True)

        # Just send a simple message to test if basic functionality works
        await interaction.channel.send("Hello! This is a forced chat test. How is everyone doing?")

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)


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

    # Check Gemini API
    if config.USE_GEMINI:
        diagnostics.append(f"✅ Using Gemini API (Model: {config.GEMINI_MODEL})")
        has_key = bool(config.GEMINI_API_KEY)
        diagnostics.append(f"{'✅' if has_key else '❌'} Gemini API Key: {'Present' if has_key else 'Missing'}")
    else:
        diagnostics.append("✅ Using Ollama API")
        diagnostics.append(f"Ollama URLs: {', '.join(config.OLLAMA_URLS)}")

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

    # Test API call
    try:
        import google.generativeai as genai

        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(model_name=config.GEMINI_MODEL)
        test_response = model.generate_content("Say 'API test successful'", stream=False)
        if test_response.text:
            diagnostics.append("✅ API Test: Successful")
        else:
            diagnostics.append("❌ API Test: No response")
    except Exception as e:
        diagnostics.append(f"❌ API Test Failed: {str(e)}")

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

            Uses exec() to dynamically create a function with the EXACT signature needed,
            preserving all type annotations. The bot parameter is hidden in the closure.
            """
            # Skip interaction and bot parameters, get the rest
            cmd_params = params_list[2:]  # Skip interaction and bot

            if not cmd_params:
                # No extra parameters - simple case

                async def wrapper(interaction):
                    await cmd(interaction, bot_instance)

                return wrapper

            # Build parameter signature string for exec()
            param_strs = []
            for p in cmd_params:
                ptype = p.annotation if p.annotation != inspect.Parameter.empty else str
                # Get type name
                type_name = getattr(ptype, "__name__", str(ptype))
                if p.default == inspect.Parameter.empty:
                    param_strs.append(f"{p.name}: {type_name}")
                else:
                    # Handle default values
                    is_simple_type = isinstance(p.default, (str, int, float, bool, type(None)))
                    default_repr = repr(p.default) if is_simple_type else "None"
                    param_strs.append(f"{p.name}: {type_name} = {default_repr}")

            params_sig = ", ".join(param_strs)
            call_args = ", ".join([p.name for p in cmd_params])

            # Create function code
            func_code = f"""
async def wrapper(interaction, {params_sig}):
    await cmd(interaction, bot_instance, {call_args})
"""

            # Execute the code to create the function
            local_vars = {"cmd": cmd, "bot_instance": bot_instance}
            exec(func_code, {}, local_vars)
            wrapper = local_vars["wrapper"]

            return wrapper

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
