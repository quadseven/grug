"""GrugThink Management API package."""

from .server import create_app, main, run_server

__all__ = ["create_app", "run_server", "main"]

# Backward compatibility - expose app creation
app = None  # Will be set when server is initialized
