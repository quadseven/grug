"""Pydantic models for API requests and responses."""

from typing import Any, Dict, Optional

from pydantic import BaseModel


class CreateBotRequest(BaseModel):
    """Request model for creating a new bot instance."""

    name: str
    template_id: str
    discord_token_id: str
    gemini_api_key: Optional[str] = None
    ollama_urls: Optional[str] = None
    ollama_models: Optional[str] = None
    google_api_key: Optional[str] = None
    google_cse_id: Optional[str] = None
    trusted_user_ids: Optional[str] = None
    custom_env: Dict[str, str] = {}


class UpdateBotRequest(BaseModel):
    """Request model for updating bot configuration."""

    name: Optional[str] = None
    discord_token: Optional[str] = None
    gemini_api_key: Optional[str] = None
    ollama_urls: Optional[str] = None
    ollama_models: Optional[str] = None
    google_api_key: Optional[str] = None
    google_cse_id: Optional[str] = None
    personality: Optional[str] = None
    force_personality: Optional[str] = None
    load_embedder: Optional[bool] = None
    log_level: Optional[str] = None
    trusted_user_ids: Optional[str] = None


class ConfigUpdateRequest(BaseModel):
    """Request model for configuration updates."""

    key: str
    value: Any


class AddDiscordTokenRequest(BaseModel):
    """Request model for adding a Discord token."""

    name: str
    token: str


class SetApiKeyRequest(BaseModel):
    """Request model for setting API keys."""

    service: str
    key_name: str
    value: str


class BotActionResponse(BaseModel):
    """Response model for bot actions."""

    success: bool
    message: str
    bot_id: Optional[str] = None


class SystemStatsResponse(BaseModel):
    """Response model for system statistics."""

    total_bots: int
    running_bots: int
    total_guilds: int
    uptime: float
    memory_usage: float
    api_calls_today: int
