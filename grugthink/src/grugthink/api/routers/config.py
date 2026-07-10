"""Configuration management API endpoints."""

from typing import Dict

from fastapi import APIRouter, Depends, HTTPException

from ...config.manager import ConfigManager
from ...logging_config import get_logger
from ..dependencies import admin_required, get_config_manager
from ..middleware import cache_response, clear_cache
from ..models import AddDiscordTokenRequest, ConfigUpdateRequest, SetApiKeyRequest

router = APIRouter(prefix="/api", tags=["config"])
log = get_logger(__name__)


@router.get("/config", dependencies=[Depends(admin_required)])
async def get_config(config_manager: ConfigManager = Depends(get_config_manager)):
    """Get current configuration."""
    return config_manager.get_config()


@router.put("/config", response_model=Dict[str, str], dependencies=[Depends(admin_required)])
async def update_config(request: ConfigUpdateRequest, config_manager: ConfigManager = Depends(get_config_manager)):
    """Update configuration value."""
    try:
        config_manager.set_config(request.key, request.value)
        return {"status": "success", "message": "Configuration updated"}
    except Exception as e:
        log.error("Failed to update config", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/templates/sync", dependencies=[Depends(admin_required)])
async def sync_personalities_to_templates(config_manager: ConfigManager = Depends(get_config_manager)):
    """Automatically create templates for personalities that don't have them."""
    try:
        config_manager.sync_personalities_to_templates()
        # Clear cache to refresh template list
        clear_cache()
        return {"message": "Personalities synchronized to templates successfully"}
    except Exception as e:
        log.error("Failed to sync personalities to templates", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to sync: {str(e)}")


@router.get("/templates", dependencies=[Depends(admin_required)])
@cache_response(ttl=300)  # Cache for 5 minutes
async def list_templates(config_manager: ConfigManager = Depends(get_config_manager)):
    """List available bot templates."""
    templates = config_manager.list_templates()
    return {
        template_id: {
            "name": template.name,
            "description": template.description,
            "force_personality": template.get_personality(),  # Use unified method
            "load_embedder": template.load_embedder,
        }
        for template_id, template in templates.items()
    }


@router.post("/discord-tokens", response_model=Dict[str, str], dependencies=[Depends(admin_required)])
async def add_discord_token(
    request: AddDiscordTokenRequest, config_manager: ConfigManager = Depends(get_config_manager)
):
    """Add a Discord bot token."""
    try:
        token_id = config_manager.add_discord_token(request.name, request.token)
        # Clear cache to ensure immediate visibility
        clear_cache()
        return {"status": "success", "token_id": token_id}
    except Exception as e:
        log.error("Failed to add Discord token", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/discord-tokens", dependencies=[Depends(admin_required)])
@cache_response(ttl=60)  # Cache for 1 minute
async def list_discord_tokens(config_manager: ConfigManager = Depends(get_config_manager)):
    """List Discord tokens (without revealing actual tokens)."""
    tokens = config_manager.get_discord_tokens()
    return [
        {"id": token["id"], "name": token["name"], "added_at": token["added_at"], "active": token.get("active", True)}
        for token in tokens
    ]


@router.delete("/discord-tokens/{token_id}", dependencies=[Depends(admin_required)])
async def delete_discord_token(token_id: str, config_manager: ConfigManager = Depends(get_config_manager)):
    """Delete a stored Discord bot token."""
    if not config_manager.remove_discord_token(token_id):
        raise HTTPException(status_code=404, detail="Token not found")
    # Clear cache to ensure immediate visibility
    clear_cache()
    return {"status": "success"}


@router.post("/api-keys", response_model=Dict[str, str], dependencies=[Depends(admin_required)])
async def set_api_key(request: SetApiKeyRequest, config_manager: ConfigManager = Depends(get_config_manager)):
    """Set API key for a service."""
    try:
        config_manager.set_api_key(request.service, request.key_name, request.value)
        return {"status": "success", "message": f"{request.service} API key updated"}
    except Exception as e:
        log.error("Failed to set API key", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api-keys/{service}", dependencies=[Depends(admin_required)])
async def get_api_keys(service: str, config_manager: ConfigManager = Depends(get_config_manager)):
    """Get API keys for a service (without revealing values)."""
    keys = config_manager.get_api_keys(service)
    # Return structure without actual key values
    return {key: "***REDACTED***" if value else None for key, value in keys.items()}


@router.get("/settings", dependencies=[Depends(admin_required)])
async def get_settings(config_manager: ConfigManager = Depends(get_config_manager)):
    """Get current system settings for the Settings page."""
    try:
        # Get environment variables from config
        env_vars = config_manager.get_environment_config()

        # Determine LLM provider based on current configuration
        gemini_key = config_manager.get_api_keys("gemini").get("primary")
        ollama_urls = env_vars.get("OLLAMA_URLS", "")
        ollama_models = env_vars.get("OLLAMA_MODELS", "llama3.2:3b")
        default_model = env_vars.get("DEFAULT_MODEL", "llama3.2:latest")

        # Determine active provider
        llm_provider = "gemini" if gemini_key else "ollama"

        return {
            "llm_provider": llm_provider,
            "gemini": {"api_key_set": bool(gemini_key), "model": env_vars.get("GEMINI_MODEL", "gemini-pro")},
            "ollama": {"urls": ollama_urls, "models": ollama_models},
            "google_search": {
                "api_key_set": bool(config_manager.get_api_keys("google_search").get("api_key")),
                "cse_id_set": bool(config_manager.get_api_keys("google_search").get("cse_id")),
            },
            "default_model": default_model,
        }
    except Exception as e:
        log.error("Failed to get settings", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/settings", response_model=Dict[str, str], dependencies=[Depends(admin_required)])
async def update_settings(request: Dict, config_manager: ConfigManager = Depends(get_config_manager)):
    """Update system settings from the Settings page."""
    try:
        llm_provider = request.get("llm_provider")

        if llm_provider == "gemini":
            # User selected Gemini - update API key if provided
            gemini_key = request.get("gemini", {}).get("api_key")
            if gemini_key and gemini_key != "***REDACTED***":  # Only update if new key provided
                config_manager.set_api_key("gemini", "primary", gemini_key)

            # Update Gemini model if provided
            gemini_model = request.get("gemini", {}).get("model")
            if gemini_model:
                config_manager.set_env_var("GEMINI_MODEL", gemini_model)

        elif llm_provider == "ollama":
            # User selected Ollama - update URLs and models
            ollama_urls = request.get("ollama", {}).get("urls")
            if ollama_urls:
                config_manager.set_env_var("OLLAMA_URLS", ollama_urls)

            ollama_models = request.get("ollama", {}).get("models")
            if ollama_models:
                config_manager.set_env_var("OLLAMA_MODELS", ollama_models)

        # Update Google Search settings if provided
        google_search = request.get("google_search", {})
        google_api_key = google_search.get("api_key")
        if google_api_key and google_api_key != "***REDACTED***":
            config_manager.set_api_key("google_search", "api_key", google_api_key)

        google_cse_id = google_search.get("cse_id")
        if google_cse_id and google_cse_id != "***REDACTED***":
            config_manager.set_api_key("google_search", "cse_id", google_cse_id)

        # Update default model if provided
        default_model = request.get("default_model")
        if default_model:
            config_manager.set_env_var("DEFAULT_MODEL", default_model)

        # Clear cache to ensure updated settings are reflected immediately
        clear_cache()

        return {"status": "success", "message": "Settings updated successfully"}
    except Exception as e:
        log.error("Failed to update settings", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ollama/models", dependencies=[Depends(admin_required)])
async def get_ollama_models(url: str):
    """Query an Ollama server for available models (proxy to avoid CORS).

    SSRF hardening: this is admin-only (the primary control - only a trusted
    dashboard admin can reach it), and the target must be a plain http(s) URL.
    We deliberately do NOT block private/loopback addresses: Ollama legitimately
    runs on localhost or an in-cluster host, so blocking those would break the
    real use case. The error is generic so we don't leak the target or internals
    back to the caller.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="url must be a http(s) URL")
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(f"{url.rstrip('/')}/api/tags", timeout=5.0)
            response.raise_for_status()
            data = response.json()
            return {"models": [m["name"] for m in data.get("models", [])]}
    except Exception as e:
        log.error("Failed to query Ollama models", extra={"error": str(e), "url": url})
        raise HTTPException(status_code=502, detail="Failed to query the Ollama server")
