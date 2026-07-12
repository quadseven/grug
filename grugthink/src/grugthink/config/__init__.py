#!/usr/bin/env python3
"""
Configuration Management Package

Manages dynamic configuration with hot-reloading, bot templates, personalities,
and Discord tokens.
"""

from .manager import ConfigManager
from .models import ConfigTemplate

__all__ = ["ConfigManager", "ConfigTemplate"]
