#!/usr/bin/env python3
"""
Download script for GrugThink embedding models.
This can be run separately to pre-download models for offline use.
"""

import os
import sys

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from .grug_db import download_model
from .grug_structured_logger import get_logger

log = get_logger(__name__)


def main():
    """Download the default embedding model."""
    print("🤖 Downloading GrugThink embedding model...")

    # Set up basic environment
    os.environ.setdefault("DISCORD_TOKEN", "dummy_token")
    os.environ.setdefault("GEMINI_API_KEY", "dummy_key")

    try:
        success = download_model("all-MiniLM-L6-v2")
        if success:
            print("✅ Model downloaded successfully!")
            print("🧠 GrugThink is ready for semantic search!")
        else:
            print("❌ Model download failed!")
            print("⚠️  Semantic search will be disabled.")
            return 1
    except Exception as e:
        print(f"❌ Error downloading model: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
