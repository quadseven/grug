"""Bot package for GrugThink Discord bot components.

This package contains helper modules for the bot (commands, cross_bot, prompts, etc.).
The main GrugThinkBot class lives in grugthink.discord_bot.
"""

# Note: Submodules are NOT imported here to avoid early initialization of Discord commands
# Import them explicitly when needed: from grugthink.bot import commands

__all__ = ["commands", "cross_bot", "llm_clients", "lore", "prompts", "review_relay", "task_relay", "utils"]
