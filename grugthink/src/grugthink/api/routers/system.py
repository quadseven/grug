"""System information and health check API endpoints."""

import logging
from datetime import datetime
from typing import Set

from fastapi import APIRouter, Depends

from ...__version__ import __build_hash__, __version__
from ...bot_manager import BotManager
from ...logging_config import get_logger
from ..dependencies import admin_required, get_bot_manager
from ..models import SystemStatsResponse

router = APIRouter(prefix="/api", tags=["system"])
log = get_logger(__name__)


@router.get("/health")
async def health_check():
    """Health check endpoint for Docker and monitoring."""
    return {
        "status": "healthy",
        "service": "grugthink-api",
        "version": __version__,
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/version")
async def get_version():
    """Get current application version and build information."""
    return {
        "version": __version__,
        "build": __build_hash__,
        "service": "grugthink-api",
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/metrics")
async def get_metrics(bot_manager: BotManager = Depends(get_bot_manager)):
    """Get basic system metrics."""
    bots = bot_manager.list_bots()
    running_bots = [bot for bot in bots if bot["status"] == "running"]

    guild_ids: Set[int] = set()
    for bot in running_bots:
        guild_ids.update(bot.get("guild_ids", []))

    return {
        "total_bots": len(bots),
        "running_bots": len(running_bots),
        "total_guilds": len(guild_ids),
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/system/stats", response_model=SystemStatsResponse, dependencies=[Depends(admin_required)])
async def get_system_stats(bot_manager: BotManager = Depends(get_bot_manager)):
    """Get system statistics."""
    bots = bot_manager.list_bots()
    running_bots = [bot for bot in bots if bot["status"] == "running"]

    guild_ids: Set[int] = set()
    for bot in running_bots:
        guild_ids.update(bot.get("guild_ids", []))

    total_guilds = len(guild_ids)

    return SystemStatsResponse(
        total_bots=len(bots),
        running_bots=len(running_bots),
        total_guilds=total_guilds,
        uptime=0.0,  # TODO: Implement uptime tracking
        memory_usage=0.0,  # TODO: Implement memory monitoring
        api_calls_today=0,  # TODO: Implement API call tracking
    )


@router.get("/system/logs")
async def get_system_logs():
    """Get recent system logs."""
    for handler in logging.getLogger().handlers:
        if hasattr(handler, "get_recent_logs"):
            return {"logs": handler.get_recent_logs()}
    return {"logs": []}


@router.get("/debug/test")
async def debug_test():
    """Simple test endpoint to verify route registration."""
    return {"message": "Test endpoint working"}


@router.get("/debug/routes")
async def list_routes():
    """Debug endpoint to list all routes (requires app context)."""
    # This endpoint needs to be registered differently to access app.routes
    return {"message": "Use app.routes to list all routes"}
