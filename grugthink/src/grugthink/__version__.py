#!/usr/bin/env python3
"""
GrugThink Version Information

Single source of truth for version numbering across the entire application.
"""

import os
from pathlib import Path


def get_version():
    """Get the current version from VERSION file."""
    try:
        # Try to read from VERSION file in repo root
        version_file = Path(__file__).parent.parent.parent / "VERSION"
        if version_file.exists():
            return version_file.read_text().strip()
    except Exception:
        pass

    # Fallback version
    return "2.0.0"


def get_build_hash():
    """Get the current git commit hash."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
            # Bounded (#712; ported from the standalone repo's last fix
            # wave): a hung git (e.g. stale index lock) must fall through
            # to the BUILD_HASH env fallback, not block module import.
            timeout=2.0,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    # Try to read from environment variable (set during Docker build)
    build_hash = os.getenv("BUILD_HASH", "unknown")
    return build_hash


# Make version available as module attribute
__version__ = get_version()
__build_hash__ = get_build_hash()
