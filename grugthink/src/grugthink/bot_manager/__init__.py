#!/usr/bin/env python3
"""
Bot Manager Package

Manages multiple Discord bot instances with different personalities and configurations.
"""

from .manager import BotManager
from .models import BotConfig, BotInstance

__all__ = ["BotManager", "BotConfig", "BotInstance"]
