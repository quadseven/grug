"""Personality management API endpoints."""

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from ...config.manager import ConfigManager
from ...logging_config import get_logger
from ..dependencies import admin_required, get_config_manager

router = APIRouter(prefix="/api/personalities", tags=["personalities"])
log = get_logger(__name__)


@router.get("", dependencies=[Depends(admin_required)])
async def get_personalities(config_manager: ConfigManager = Depends(get_config_manager)):
    """Get all available personalities."""
    personalities = config_manager.list_personalities()
    return {"personalities": personalities}


@router.get("/{personality_id}", dependencies=[Depends(admin_required)])
async def get_personality(personality_id: str, config_manager: ConfigManager = Depends(get_config_manager)):
    """Get a specific personality configuration."""
    personality = config_manager.get_personality(personality_id)
    if not personality:
        raise HTTPException(status_code=404, detail=f"Personality '{personality_id}' not found")
    return {"personality": personality}


@router.post("/generate", dependencies=[Depends(admin_required)])
async def generate_personality_with_ai(
    request: Dict[str, str], config_manager: ConfigManager = Depends(get_config_manager)
):
    """Generate a personality using Gemini AI based on user description."""
    description = request.get("description", "").strip()
    personality_id = request.get("personality_id", "").strip()

    if not description or not personality_id:
        raise HTTPException(status_code=400, detail="Both 'description' and 'personality_id' are required")

    # Check if personality already exists
    existing = config_manager.get_personality(personality_id)
    if existing:
        raise HTTPException(status_code=409, detail=f"Personality '{personality_id}' already exists")

    try:
        # Gemini is intentionally removed in v2 - see requirements.txt
        raise HTTPException(status_code=501, detail="Gemini AI is disabled in this version")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate personality: {str(e)}")


@router.post("/{personality_id}", dependencies=[Depends(admin_required)])
async def create_personality(
    personality_id: str, personality_config: Dict[str, Any], config_manager: ConfigManager = Depends(get_config_manager)
):
    """Create a new personality configuration."""
    # Check if personality already exists
    existing = config_manager.get_personality(personality_id)
    if existing:
        raise HTTPException(status_code=409, detail=f"Personality '{personality_id}' already exists")

    success = config_manager.add_personality(personality_id, personality_config)
    if success:
        # Auto-sync personalities to templates
        config_manager.sync_personalities_to_templates()
        return {"message": f"Personality '{personality_id}' created successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to create personality")


@router.put("/{personality_id}", dependencies=[Depends(admin_required)])
async def update_personality(
    personality_id: str, updates: Dict[str, Any], config_manager: ConfigManager = Depends(get_config_manager)
):
    """Update a personality configuration."""
    success = config_manager.update_personality(personality_id, updates)
    if success:
        return {"message": f"Personality '{personality_id}' updated successfully"}
    else:
        raise HTTPException(status_code=404, detail=f"Personality '{personality_id}' not found")


@router.delete("/{personality_id}", dependencies=[Depends(admin_required)])
async def delete_personality(personality_id: str, config_manager: ConfigManager = Depends(get_config_manager)):
    """Delete a personality configuration."""
    success = config_manager.remove_personality(personality_id)
    if success:
        return {"message": f"Personality '{personality_id}' deleted successfully"}
    else:
        raise HTTPException(status_code=404, detail=f"Personality '{personality_id}' not found")
