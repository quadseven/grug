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
        from .discord_bot import GrugThinkBot

        return GrugThinkBot

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
