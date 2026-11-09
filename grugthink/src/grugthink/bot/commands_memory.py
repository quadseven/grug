"""Admin-only memory management slash command handlers.

Split out of commands.py by command group (grug#1692).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import discord

from .. import runtime_config as config
from ..logging_config import get_logger

if TYPE_CHECKING:
    from ..discord_bot import GrugThinkBot

log = get_logger(__name__)


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
    dashboard_url = config.PUBLIC_BASE_URL
    if dashboard_url:
        embed.add_field(name="Web Interface", value=f"[Manage Memories]({dashboard_url})", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)
