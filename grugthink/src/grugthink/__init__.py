"""
GrugThink - Adaptable Discord Personality Engine

A sophisticated multi-bot container system with personality evolution,
web dashboard management, and real-time monitoring capabilities.
"""

from .__version__ import __version__ as __version__

__author__ = "GrugThink Contributors"
__description__ = "Adaptable Discord Personality Engine with Multi-Bot Container Support"

# Multi-bot system - core modules always available
from .bot_manager import BotConfig, BotInstance, BotManager
from .config.manager import ConfigManager
from .config.templates import ConfigTemplate
from .grug_db import GrugDB
from .grug_structured_logger import get_logger
from .personality_engine import PersonalityEngine, PersonalityState, PersonalityTemplate

__all__ = [
    "PersonalityEngine",
    "PersonalityTemplate",
    "PersonalityState",
    "GrugDB",
    "get_logger",
    "BotManager",
    "BotConfig",
    "BotInstance",
    "ConfigManager",
    "ConfigTemplate",
    "GrugThinkBot",
]


def __getattr__(name: str):
    """Lazy import optional submodules."""
    if name == "bot":
        import importlib

        _bot = importlib.import_module(".bot", __name__)
        return _bot
    elif name == "GrugThinkBot":
        # GrugThinkBot is in bot.py but shadowed by bot/ package
        # Import using a sys.modules hack to load bot.py under an alternate name
        import importlib.util
        import os
        import sys

        # Check if already loaded under alternate name
        alt_name = "grugthink.bot_module"
        if alt_name in sys.modules:
            return sys.modules[alt_name].GrugThinkBot

        # Load bot.py under alternate module name to avoid bot/ package conflict
        bot_py_path = os.path.join(os.path.dirname(__file__), "bot.py")
        spec = importlib.util.spec_from_file_location(alt_name, bot_py_path)
        if spec and spec.loader:
            bot_module = importlib.util.module_from_spec(spec)
            # Register under alternate name BEFORE executing to handle circular imports
            sys.modules[alt_name] = bot_module
            try:
                spec.loader.exec_module(bot_module)
                # Also cache under the expected name for future imports
                return bot_module.GrugThinkBot
            except Exception:
                # Clean up on failure
                sys.modules.pop(alt_name, None)
                raise

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
