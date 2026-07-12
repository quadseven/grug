#!/usr/bin/env python3
"""
Configuration Data Models

Defines ConfigTemplate dataclass for bot configuration templates.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ConfigTemplate:
    """Template for creating bot configurations."""

    name: str
    description: str
    personality: Optional[str] = None  # References personality config
    force_personality: Optional[str] = None  # Deprecated, use personality instead
    load_embedder: bool = False  # Default to False to avoid memory issues in containers
    default_gemini_key: bool = True
    default_google_search: bool = False
    default_ollama: bool = False
    custom_env: Dict[str, str] = field(default_factory=dict)

    def get_personality(self) -> Optional[str]:
        """Get the personality for this template, checking both new and deprecated fields."""
        return self.personality or self.force_personality
