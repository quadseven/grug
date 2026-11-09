"""User-facing slash command handlers (verify, learn, help and similar).

Split out of commands.py by command group (grug#1692); commands.py registers them.
"""

from __future__ import annotations

import asyncio
import traceback
from typing import TYPE_CHECKING

import discord

from .. import runtime_config as config
from ..bot.prompts import is_rate_limited, query_model
from ..logging_config import get_logger

if TYPE_CHECKING:
    from ..discord_bot import GrugThinkBot

log = get_logger(__name__)


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
        # to_thread copies the context, so query_model reads this bot's settings.
        result = await asyncio.to_thread(
            query_model, target, server_db, server_id, bot.personality_engine, bot.get_bot_id()
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


_FACTS_PER_PAGE = 15
_MAX_FACT_LENGTH = 120  # Max length per individual fact


def _no_facts_message(personality, bot_name: str, search: str | None) -> str:
    """Text sent when the fact list (or search result) is empty."""
    if search:
        return f"No facts found matching '{search}'"
    if personality.response_style == "caveman":
        return f"{bot_name} know nothing in this cave."
    if personality.response_style == "british_working_class":
        return "dont know nuffin yet mate, simple as"
    return "I don't know any facts yet."


def _page_heading(personality, bot_name: str, search: str | None, total_facts: int) -> tuple[str, str]:
    """Return (base_title, total_description) for the what-know embed."""
    if search:
        return f"Search: '{search}'", f"Found {total_facts} matching facts"
    if personality.response_style == "caveman":
        return f"{bot_name}'s Memories", f"{bot_name} knows {total_facts} things in this cave"
    if personality.response_style == "british_working_class":
        return "wot i know", f"got {total_facts} fings in me ed, nuff said"
    return "Knowledge Base", f"I know {total_facts} facts"


def _nav_hints(page: int, total_pages: int, search: str | None) -> list[str]:
    """Navigation hints shown under the fact list."""
    hints = []
    if page > 1:
        hints.append(f"Previous: `/what-know page:{page - 1}`")
    if page < total_pages:
        hints.append(f"Next: `/what-know page:{page + 1}`")
    hints.append("All facts: `/what-know`" if search else "Search: `/what-know search:term`")
    return hints


def _format_fact_list(page_facts, start_idx: int) -> str:
    """Numbered, length-truncated lines for one page of facts."""
    lines = []
    for i, fact in enumerate(page_facts, start=start_idx + 1):
        display_fact = fact[:_MAX_FACT_LENGTH] + "..." if len(fact) > _MAX_FACT_LENGTH else fact
        lines.append(f"{i}. {display_fact}")
    return "\n".join(lines)


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

    server_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    personality = bot.personality_engine.get_personality(server_id)
    bot_name = personality.chosen_name or personality.name
    server_db = bot.get_server_db(interaction)

    if search:
        all_facts = server_db.search_facts(search, k=50)  # Search with higher limit
    else:
        all_facts = server_db.get_all_facts()

    if not all_facts:
        await interaction.followup.send(_no_facts_message(personality, bot_name, search), ephemeral=True)
        return

    total_facts = len(all_facts)
    total_pages = max(1, (total_facts + _FACTS_PER_PAGE - 1) // _FACTS_PER_PAGE)
    page = min(max(page, 1), total_pages)
    start_idx = (page - 1) * _FACTS_PER_PAGE
    page_facts = all_facts[start_idx : min(start_idx + _FACTS_PER_PAGE, total_facts)]

    server_name = interaction.guild.name if interaction.guild else "DM"
    base_title, description = _page_heading(personality, bot_name, search, total_facts)
    title = f"{base_title} ({server_name}) - Page {page}/{total_pages}"
    dashboard_url = config.PUBLIC_BASE_URL
    if dashboard_url:
        description += f"\n\U0001f310 **[View & Edit All Memories]({dashboard_url})**"

    fact_list = _format_fact_list(page_facts, start_idx)
    fact_list += f"\n\n\U0001f4dd {' | '.join(_nav_hints(page, total_pages, search))}"

    embed = discord.Embed(title=title, description=description, color=discord.Color.blue())
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
