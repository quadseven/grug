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
        # Import Gemini functionality
        import google.generativeai as genai

        # Get Gemini API key from ConfigManager instead of environment
        gemini_keys = config_manager.get_api_keys("gemini")
        gemini_api_key = gemini_keys.get("primary")

        if not gemini_api_key:
            raise HTTPException(status_code=500, detail="Gemini API key not configured")

        genai.configure(api_key=gemini_api_key)

        # Get model from config or use default
        gemini_model = config_manager.get_env_var("GEMINI_MODEL", "gemini-pro")
        model = genai.GenerativeModel(gemini_model)

        # Create the prompt for personality generation
        prompt = f"""You are an expert YAML personality designer for Discord chatbots.
Create a personality YAML configuration based on this description: "{description}"

Your output must follow this EXACT structure and format. Be creative but maintain the structure:

```yaml
name: "Character Name Here"
description: "Brief description of the character"

behavior:
  emotions:
    confused: "How they express confusion"
    excited: "How they express excitement"
    happy: "How they express happiness"
    sad: "How they express sadness"
  response_patterns:
    agreement: "How they agree with something"
    confusion: "How they ask for clarification"
    disagreement: "How they disagree"
    farewell: "How they say goodbye"
    greeting: "How they greet people"
    learning: "How they respond when learning something new"

speech:
  catchphrases:
    - "First catchphrase"
    - "Second catchphrase"
    - "Third catchphrase"
    - "Fourth catchphrase"
    - "Fifth catchphrase"
  error_prefix: "What they say before errors:"
  help_prefix: "What they say before helping:"
  sentence_structure: "simple/casual/formal/complex"
  thinking_prefix: "What they say while thinking..."
  verification_prefix: "How they start factual statements:"
  vocabulary_level: "basic/colloquial/normal/advanced"
  word_replacements:
    original_word: "replacement_word"
    another_word: "replacement"

traits:
  emotional_range: "basic/normal/complex"
  humor_style: "innocent/cheeky/sarcastic/dry/playful"
  intelligence_level: "simple/average/advanced/genius"
  verbosity: "concise/normal/verbose"
```

Generate ONLY the YAML content. No explanation, no markdown formatting,
just the raw YAML that can be parsed directly."""

        # Generate personality with Gemini
        response = model.generate_content(prompt)
        generated_yaml = response.text.strip()

        # Clean up any markdown formatting that might have leaked through
        if "```yaml" in generated_yaml:
            generated_yaml = generated_yaml.split("```yaml")[1].split("```")[0].strip()
        elif "```" in generated_yaml:
            generated_yaml = generated_yaml.split("```")[1].split("```")[0].strip()

        # Validate YAML structure
        import yaml

        try:
            personality_data = yaml.safe_load(generated_yaml)
            if not isinstance(personality_data, dict):
                raise ValueError("Generated content is not a valid YAML object")

            # Ensure required fields exist
            required_fields = ["name", "description", "behavior", "speech", "traits"]
            for field in required_fields:
                if field not in personality_data:
                    raise ValueError(f"Missing required field: {field}")

        except yaml.YAMLError as e:
            raise HTTPException(status_code=500, detail=f"Generated YAML is invalid: {str(e)}")
        except ValueError as e:
            raise HTTPException(status_code=500, detail=f"Generated content validation failed: {str(e)}")

        # Save the personality
        success = config_manager.add_personality(personality_id, personality_data)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to save generated personality")

        # Also save to personalities directory as YAML file
        personalities_dir = "personalities"
        if not os.path.exists(personalities_dir):
            os.makedirs(personalities_dir)

        yaml_file_path = os.path.join(personalities_dir, f"{personality_id}.yaml")
        with open(yaml_file_path, "w") as f:
            f.write(generated_yaml)

        # Auto-sync personalities to templates
        config_manager.sync_personalities_to_templates()

        return {
            "message": f"Personality '{personality_id}' generated and saved successfully",
            "personality_id": personality_id,
            "generated_yaml": generated_yaml,
            "personality_data": personality_data,
        }

    except ImportError:
        raise HTTPException(status_code=500, detail="Gemini AI not available - missing google-generativeai package")
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
