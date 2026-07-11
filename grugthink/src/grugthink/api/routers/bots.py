"""Bot management API endpoints."""

import logging
from typing import Any, Dict, List, Union

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from ...bot_manager import BotManager
from ...config.manager import ConfigManager
from ...logging_config import get_logger
from ..dependencies import admin_required, get_bot_manager, get_config_manager
from ..middleware import clear_cache
from ..models import BotActionResponse, CreateBotRequest, UpdateBotRequest

router = APIRouter(prefix="/api/bots", tags=["bots"])
log = get_logger(__name__)


@router.get("", response_model=List[Dict[str, Any]], dependencies=[Depends(admin_required)])
async def list_bots(bot_manager: BotManager = Depends(get_bot_manager)):
    """List all bot configurations and their status."""
    return bot_manager.list_bots()


@router.get("/{bot_id}", dependencies=[Depends(admin_required)])
async def get_bot(bot_id: str, bot_manager: BotManager = Depends(get_bot_manager)):
    """Get specific bot status and configuration."""
    bot_status = bot_manager.get_bot_status(bot_id)
    if not bot_status:
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot_status


@router.post("", response_model=BotActionResponse, dependencies=[Depends(admin_required)])
async def create_bot(
    request: CreateBotRequest,
    bot_manager: BotManager = Depends(get_bot_manager),
    config_manager: ConfigManager = Depends(get_config_manager),
):
    """Create a new bot instance."""
    try:
        # Get template and create environment
        template = config_manager.get_template(request.template_id)
        if not template:
            raise HTTPException(status_code=400, detail=f"Template '{request.template_id}' not found")

        # Retrieve the actual Discord token using the token ID
        discord_token = config_manager.get_discord_token_by_id(request.discord_token_id)
        if not discord_token:
            raise HTTPException(
                status_code=400, detail=f"Discord token with ID '{request.discord_token_id}' not found or inactive."
            )

        # Create bot environment from template
        config_manager.create_bot_env(request.template_id, discord_token, **request.custom_env)

        # Override with specific values if provided
        overrides = {}
        if request.gemini_api_key:
            overrides["gemini_api_key"] = request.gemini_api_key
        if request.ollama_urls:
            overrides["ollama_urls"] = request.ollama_urls
        if request.ollama_models:
            overrides["ollama_models"] = request.ollama_models
        if request.google_api_key:
            overrides["google_api_key"] = request.google_api_key
        if request.google_cse_id:
            overrides["google_cse_id"] = request.google_cse_id
        if request.trusted_user_ids:
            overrides["trusted_user_ids"] = request.trusted_user_ids

        # Extract template settings
        template_dict = template if isinstance(template, dict) else template.__dict__

        bot_id = bot_manager.create_bot(
            name=request.name,
            discord_token_id=request.discord_token_id,
            template_id=request.template_id,
            personality=template_dict.get("personality"),
            load_embedder=template_dict.get("load_embedder", False),  # Default to False to avoid memory issues
            **overrides,
        )

        # Clear cache to ensure immediate visibility
        clear_cache()

        return BotActionResponse(success=True, message=f"Bot '{request.name}' created successfully", bot_id=bot_id)

    except Exception as e:
        log.error("Failed to create bot", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{bot_id}", response_model=BotActionResponse, dependencies=[Depends(admin_required)])
async def update_bot(bot_id: str, request: UpdateBotRequest, bot_manager: BotManager = Depends(get_bot_manager)):
    """Update bot configuration."""
    try:
        updates = {}
        for field, value in request.dict(exclude_unset=True).items():
            if value is not None:
                updates[field] = value

        success = bot_manager.update_bot_config(bot_id, **updates)
        if not success:
            raise HTTPException(status_code=404, detail="Bot not found")

        # Clear cache to ensure immediate visibility
        clear_cache()

        return BotActionResponse(success=True, message="Bot configuration updated successfully", bot_id=bot_id)

    except Exception as e:
        log.error("Failed to update bot", extra={"bot_id": bot_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{bot_id}", response_model=BotActionResponse, dependencies=[Depends(admin_required)])
async def delete_bot(bot_id: str, bot_manager: BotManager = Depends(get_bot_manager)):
    """Delete a bot instance."""
    try:
        success = await bot_manager.delete_bot(bot_id)
        if not success:
            raise HTTPException(status_code=404, detail="Bot not found")

        # Clear cache to ensure immediate visibility
        clear_cache()

        return BotActionResponse(success=True, message="Bot deleted successfully", bot_id=bot_id)

    except Exception as e:
        log.error("Failed to delete bot", extra={"bot_id": bot_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


async def _start_bot_task(bot_id: str, bot_manager: BotManager):
    """Background task to start a bot."""
    try:
        await bot_manager.start_bot(bot_id)
    except Exception as e:
        log.error("Error in start bot task", extra={"bot_id": bot_id, "error": str(e)})


@router.post("/{bot_id}/start", response_model=BotActionResponse, dependencies=[Depends(admin_required)])
async def start_bot(bot_id: str, background_tasks: BackgroundTasks, bot_manager: BotManager = Depends(get_bot_manager)):
    """Start a bot instance."""
    try:
        background_tasks.add_task(_start_bot_task, bot_id, bot_manager)
        return BotActionResponse(success=True, message="Bot start initiated", bot_id=bot_id)
    except Exception as e:
        log.error("Failed to start bot", extra={"bot_id": bot_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


async def _stop_bot_task(bot_id: str, bot_manager: BotManager):
    """Background task to stop a bot."""
    try:
        await bot_manager.stop_bot(bot_id)
    except Exception as e:
        log.error("Error in stop bot task", extra={"bot_id": bot_id, "error": str(e)})


@router.post("/{bot_id}/stop", response_model=BotActionResponse, dependencies=[Depends(admin_required)])
async def stop_bot(bot_id: str, background_tasks: BackgroundTasks, bot_manager: BotManager = Depends(get_bot_manager)):
    """Stop a bot instance."""
    try:
        background_tasks.add_task(_stop_bot_task, bot_id, bot_manager)
        return BotActionResponse(success=True, message="Bot stop initiated", bot_id=bot_id)
    except Exception as e:
        log.error("Failed to stop bot", extra={"bot_id": bot_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


async def _restart_bot_task(bot_id: str, bot_manager: BotManager):
    """Background task to restart a bot."""
    try:
        await bot_manager.restart_bot(bot_id)
    except Exception as e:
        log.error("Error in restart bot task", extra={"bot_id": bot_id, "error": str(e)})


@router.post("/{bot_id}/restart", response_model=BotActionResponse, dependencies=[Depends(admin_required)])
async def restart_bot(
    bot_id: str, background_tasks: BackgroundTasks, bot_manager: BotManager = Depends(get_bot_manager)
):
    """Restart a bot instance."""
    try:
        background_tasks.add_task(_restart_bot_task, bot_id, bot_manager)
        return BotActionResponse(success=True, message="Bot restart initiated", bot_id=bot_id)
    except Exception as e:
        log.error("Failed to restart bot", extra={"bot_id": bot_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{bot_id}/chat-settings", dependencies=[Depends(admin_required)])
async def get_bot_chat_settings(
    bot_id: str, server_id: str = "admin", bot_manager: BotManager = Depends(get_bot_manager)
):
    """Get chat frequency and activity settings for a bot."""
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"Bot '{bot_id}' not found")

    # Get the GrugThinkBot cog to access chat settings
    cog = None
    if bot.client and bot.client.cogs:
        for cog_instance in bot.client.cogs.values():
            if hasattr(cog_instance, "chat_frequencies"):
                cog = cog_instance
                break

    if not cog:
        return {
            "chat_frequency": 0,
            "activity_data": {},
            "channel_activity": {},
            "bot_id": bot_id,
            "server_id": server_id,
            "message": "Bot cog not loaded or bot not running",
        }

    # Get chat frequency for the server
    chat_frequency = cog.chat_frequencies.get(server_id, 0)

    # Get activity data if available (simplified for API)
    activity_summary = {}
    if hasattr(cog, "channel_activity"):
        for channel_id, activity in cog.channel_activity.items():
            activity_summary[channel_id] = {
                "last_human_message": activity.get("last_human_message", 0),
                "last_bot_message": activity.get("last_bot_message", 0),
                "message_count": activity.get("message_count", 0),
            }

    return {
        "chat_frequency": chat_frequency,
        "activity_data": activity_summary,
        "chat_frequencies_count": len(cog.chat_frequencies) if hasattr(cog, "chat_frequencies") else 0,
        "bot_id": bot_id,
        "server_id": server_id,
    }


@router.post("/{bot_id}/chat-frequency", dependencies=[Depends(admin_required)])
async def set_bot_chat_frequency(
    bot_id: str, request: Dict[str, Union[str, int]], bot_manager: BotManager = Depends(get_bot_manager)
):
    """Set chat frequency for a bot on a specific server."""
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"Bot '{bot_id}' not found")

    server_id = request.get("server_id", "admin")
    frequency = request.get("frequency", 0)

    # Validate frequency
    try:
        frequency = int(frequency)
        if frequency < 0 or frequency > 100:
            raise HTTPException(status_code=400, detail="Frequency must be between 0 and 100")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Frequency must be a valid integer")

    # Get the GrugThinkBot cog to update chat settings
    cog = None
    if bot.client and bot.client.cogs:
        for cog_instance in bot.client.cogs.values():
            if hasattr(cog_instance, "chat_frequencies"):
                cog = cog_instance
                break

    if not cog:
        raise HTTPException(status_code=500, detail="Bot cog not loaded or bot not running")

    # Update the chat frequency
    cog.chat_frequencies[server_id] = frequency

    # Save to persistent storage if available
    if hasattr(cog, "_save_chat_frequencies"):
        cog._save_chat_frequencies()

    log.info("Chat frequency updated via API", extra={"bot_id": bot_id, "server_id": server_id, "frequency": frequency})

    return {
        "message": "Chat frequency updated successfully",
        "bot_id": bot_id,
        "server_id": server_id,
        "frequency": frequency,
    }


@router.post("/{bot_id}/reset-activity", dependencies=[Depends(admin_required)])
async def reset_bot_activity(bot_id: str, bot_manager: BotManager = Depends(get_bot_manager)):
    """Reset activity tracking data for a bot."""
    bot = bot_manager.bots.get(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"Bot '{bot_id}' not found")

    # Get the GrugThinkBot cog to reset activity
    cog = None
    if bot.client and bot.client.cogs:
        for cog_instance in bot.client.cogs.values():
            if hasattr(cog_instance, "channel_activity"):
                cog = cog_instance
                break

    if not cog:
        raise HTTPException(status_code=500, detail="Bot cog not loaded or bot not running")

    # Reset activity tracking data
    if hasattr(cog, "channel_activity"):
        cog.channel_activity.clear()

    log.info("Activity data reset via API", extra={"bot_id": bot_id, "source": "web_api"})

    return {"message": "Bot activity data reset successfully", "bot_id": bot_id}


@router.get("/{bot_id}/logs", dependencies=[Depends(admin_required)])
async def get_bot_logs(bot_id: str):
    """Get logs for a specific bot."""
    try:
        for handler in logging.getLogger().handlers:
            if hasattr(handler, "get_recent_logs"):
                all_logs = handler.get_recent_logs()
                bot_logs = [entry for entry in all_logs if entry.get("bot_id") == bot_id]

                # Clean up any problematic float values
                cleaned_logs = []
                for entry in bot_logs[-100:]:
                    cleaned_entry = {}
                    for key, value in entry.items():
                        if isinstance(value, float):
                            if value != value:  # NaN check
                                cleaned_entry[key] = None
                            elif value == float("inf") or value == float("-inf"):
                                cleaned_entry[key] = None
                            else:
                                cleaned_entry[key] = value
                        else:
                            cleaned_entry[key] = value
                    cleaned_logs.append(cleaned_entry)

                return {"logs": cleaned_logs}
        return {"logs": []}
    except Exception as e:
        # Log the detail server-side; do not leak internals to the API caller.
        log.error("Error getting bot logs", extra={"bot_id": bot_id, "error": str(e)})
        return {"logs": [], "error": "failed to retrieve bot logs"}
