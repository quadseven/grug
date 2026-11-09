"""Admin-only chat configuration and chat-test slash command handlers.

Split out of commands.py by command group (grug#1692).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import discord

from .. import runtime_config as config
from ..logging_config import get_logger

if TYPE_CHECKING:
    from ..discord_bot import GrugThinkBot

log = get_logger(__name__)


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
