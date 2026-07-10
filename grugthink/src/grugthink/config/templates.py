#!/usr/bin/env python3
"""
Template Management

Handles bot configuration templates and template-to-personality synchronization.
"""

from typing import Any, Dict, Optional

from ..grug_structured_logger import get_logger
from .models import ConfigTemplate

log = get_logger(__name__)


def create_default_templates() -> Dict[str, ConfigTemplate]:
    """Create default bot configuration templates."""
    return {
        "pure_grug": ConfigTemplate(
            name="Pure Grug",
            description="Caveman personality only, no evolution",
            force_personality="grug",
            load_embedder=True,
        ),
        "pure_big_rob": ConfigTemplate(
            name="Pure Big Rob",
            description="norf FC lad personality only, no evolution",
            force_personality="big_rob",
            load_embedder=True,
        ),
        "evolution_bot": ConfigTemplate(
            name="Evolution Bot",
            description="Adaptive personality that evolves per server",
            force_personality=None,  # No forced personality
            load_embedder=True,
        ),
        "lightweight_grug": ConfigTemplate(
            name="Lightweight Grug",
            description="Grug personality without semantic search",
            force_personality="grug",
            load_embedder=False,
        ),
        "multi_personality": ConfigTemplate(
            name="Multi-Personality",
            description="Random personality selection per server",
            force_personality=None,
            load_embedder=True,
        ),
        "ollama_bot": ConfigTemplate(
            name="Ollama Bot",
            description="Uses local Ollama instead of Gemini",
            force_personality=None,
            load_embedder=False,  # Disabled to prevent blocking on model download - Issue #41
            default_gemini_key=False,
            default_ollama=True,
            # custom_env removed - ollama URLs/models now come from global environment section
            # This prevents template defaults from overwriting configured values (Issue #46)
        ),
    }


def get_template(
    config_data: Dict[str, Any], templates: Dict[str, ConfigTemplate], template_id: str
) -> Optional[ConfigTemplate]:
    """Get bot configuration template."""
    if template_id in templates:
        return templates[template_id]

    # Check if it's in the config file
    template_data = config_data.get("bot_templates", {}).get(template_id)
    if template_data:
        return ConfigTemplate(**template_data)

    return None


def list_templates(
    config_data: Dict[str, Any], default_templates: Dict[str, ConfigTemplate]
) -> Dict[str, ConfigTemplate]:
    """List all available templates."""
    templates = {}

    # Load templates from config file first (higher priority)
    config_templates = config_data.get("bot_templates", {})
    for template_id, template_data in config_templates.items():
        try:
            templates[template_id] = ConfigTemplate(**template_data)
        except Exception as e:
            log.error("Invalid template in config", extra={"template_id": template_id, "error": str(e)})

    # Only add hardcoded templates if no config templates exist (for backwards compatibility)
    if not config_templates:
        for template_id, template in default_templates.items():
            templates[template_id] = template

    return templates


def sync_personalities_to_templates(config_manager):
    """Automatically create bot templates for personalities that don't have them."""
    personalities = config_manager.config_data.get("personalities", {})
    existing_templates = config_manager.get_config("bot_templates") or {}

    # Find personalities that don't have corresponding templates
    templates_with_personalities = set()
    for template_data in existing_templates.values():
        # Check both new and deprecated personality fields
        personality = template_data.get("personality") or template_data.get("force_personality")
        if personality:
            templates_with_personalities.add(personality)

    personalities_without_templates = set(personalities.keys()) - templates_with_personalities

    if personalities_without_templates:
        log.info(
            "Auto-creating templates for personalities",
            extra={"personalities": list(personalities_without_templates)},
        )

        for personality_id in personalities_without_templates:
            personality_data = personalities[personality_id]
            personality_name = personality_data.get("name", personality_id.replace("_", " ").title())
            personality_desc = personality_data.get("description", f"{personality_name} personality")

            # Create a template for this personality
            template_id = f"pure_{personality_id}"
            template_config = {
                "name": f"Pure {personality_name}",
                "description": f"{personality_desc}",
                "force_personality": personality_id,  # Use deprecated field for compatibility
                "personality": personality_id,  # Also include new field
                "load_embedder": True,
                "default_gemini_key": True,
                "default_google_search": False,
                "default_ollama": False,
                "custom_env": {},
            }

            existing_templates[template_id] = template_config
            log.info(
                "Created template for personality",
                extra={"template_id": template_id, "personality_id": personality_id},
            )

        # Save the updated templates
        config_manager.set_config("bot_templates", existing_templates)


def create_bot_env(config_manager, template_id: str, discord_token: str, **overrides) -> Dict[str, str]:
    """Create environment variables for a bot from template."""
    from ..grug_structured_logger import get_logger

    log = get_logger(__name__)

    template = get_template(config_manager.config_data, config_manager.templates, template_id)
    if not template:
        raise ValueError(f"Template '{template_id}' not found")

    log.debug(
        "create_bot_env: Starting environment creation",
        extra={
            "template_id": template_id,
            "template_default_ollama": template.default_ollama,
            "template_custom_env_keys": sorted(template.custom_env.keys()),
            "overrides_keys": sorted(overrides.keys()),
            "config_manager_env_vars": sorted(config_manager.env_vars.keys()),
        },
    )

    env = {}

    # Base environment from config
    env.update(config_manager.env_vars)
    log.debug(
        "create_bot_env: After base env update",
        extra={"env_keys": sorted(env.keys()), "has_ollama_urls": "OLLAMA_URLS" in env},
    )

    # Core bot settings
    env["DISCORD_TOKEN"] = discord_token
    env["LOAD_EMBEDDER"] = str(template.load_embedder)

    # Set personality (prefer new 'personality' field over deprecated 'force_personality')
    personality = getattr(template, "personality", None) or template.force_personality
    if personality:
        env["FORCE_PERSONALITY"] = personality

    # API keys
    if template.default_gemini_key:
        gemini_keys = config_manager.get_api_keys("gemini")
        primary_key = gemini_keys.get("primary")
        if primary_key:
            env["GEMINI_API_KEY"] = primary_key

    if template.default_google_search:
        google_keys = config_manager.get_api_keys("google_search")
        if google_keys.get("api_key"):
            env["GOOGLE_API_KEY"] = google_keys["api_key"]
        if google_keys.get("cse_id"):
            env["GOOGLE_CSE_ID"] = google_keys["cse_id"]

    if template.default_ollama:
        ollama_urls_value = config_manager.get_env_var("OLLAMA_URLS", "http://localhost:11434")
        ollama_models_value = config_manager.get_env_var("OLLAMA_MODELS", "llama3.2:3b")
        env["OLLAMA_URLS"] = ollama_urls_value
        env["OLLAMA_MODELS"] = ollama_models_value
        log.debug(
            "create_bot_env: Set Ollama env vars",
            extra={"OLLAMA_URLS": ollama_urls_value, "OLLAMA_MODELS": ollama_models_value, "from_get_env_var": True},
        )

    # Custom environment from template
    env.update(template.custom_env)
    log.debug(
        "create_bot_env: After template custom_env update",
        extra={
            "env_keys": sorted(env.keys()),
            "OLLAMA_URLS": env.get("OLLAMA_URLS"),
            "template_custom_env": template.custom_env,
        },
    )

    # Apply overrides
    env.update(overrides)
    log.debug(
        "create_bot_env: After overrides",
        extra={
            "env_keys": sorted(env.keys()),
            "OLLAMA_URLS": env.get("OLLAMA_URLS"),
            "OLLAMA_MODELS": env.get("OLLAMA_MODELS"),
            "overrides": overrides,
        },
    )

    return env
