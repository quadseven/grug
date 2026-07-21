"""Bot package for GrugThink Discord bot components.

This package contains helper modules for the bot (commands, cross_bot, prompts, etc.).
The main GrugThinkBot class is defined in ../bot.py (not in this package).

Due to Python's import resolution, this package shadows the bot.py module file.
To resolve the import conflict, GrugThinkBot must be imported via grugthink package:
    from grugthink import GrugThinkBot
"""

# Note: Submodules are NOT imported here to avoid early initialization of Discord commands
# Import them explicitly when needed: from grugthink.bot import commands

__all__ = ["commands", "cross_bot", "llm_clients", "lore", "prompts", "task_relay", "utils"]
