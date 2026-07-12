"""FastAPI server setup and configuration."""

import asyncio
import os
import secrets
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..__version__ import __build_hash__, __version__
from ..bot_manager import BotManager
from ..config.manager import ConfigManager
from ..logging_config import get_logger, setup_logging
from . import dependencies
from .routers import admin, bots, config, memories, personalities, system

log = get_logger(__name__)

# Check if Sentry is available
SENTRY_ENABLED = bool(os.getenv("SENTRY_DSN"))
SentryAsgiMiddleware = None
if SENTRY_ENABLED:
    try:
        from sentry_sdk.integrations.asgi import SentryAsgiMiddleware
    except ImportError:
        log.warning("Sentry SDK not available despite SENTRY_DSN being set")
        SENTRY_ENABLED = False


def create_app(bot_manager: BotManager, config_manager: ConfigManager) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="GrugThink Management API",
        description="API for managing multiple Discord bot instances",
        version=__version__,
        # Performance optimizations
        docs_url=None,  # Disable docs in production
        redoc_url=None,  # Disable redoc in production
    )

    # Set global manager instances for dependency injection
    dependencies.set_bot_manager(bot_manager)
    dependencies.set_config_manager(config_manager)

    # Session secret: NEVER ship a hardcoded default (a public repo would leak a
    # forgeable signing key). Read it from config/env only.
    session_secret = None
    if config_manager:
        session_secret = config_manager.get_env_var("SESSION_SECRET_KEY", "") or None
    if not session_secret:
        session_secret = os.getenv("SESSION_SECRET") or None

    # Only add SessionMiddleware if OAuth is enabled
    disable_oauth = False
    if config_manager:
        disable_oauth = config_manager.get_env_var("DISABLE_OAUTH", "false").lower() == "true"
    else:
        disable_oauth = os.getenv("DISABLE_OAUTH", "false").lower() == "true"

    if not session_secret and not disable_oauth:
        # Only fail if OAuth is enabled and no secret is set.
        raise RuntimeError("SESSION_SECRET (or SESSION_SECRET_KEY) must be set when OAuth is enabled")
    if not session_secret:
        # Reached only when OAuth is disabled and no secret is set: use an
        # ephemeral per-process secret (sessions do not survive a restart).
        session_secret = secrets.token_urlsafe(32)

    # Always add SessionMiddleware, but if OAuth is disabled we use an ephemeral secret (already set above if needed)
    app.add_middleware(SessionMiddleware, secret_key=session_secret)

    # CORS: restrict to a configured allowlist - never "*" with credentials.
    allowed_origins = "http://localhost:8080"
    if config_manager:
        allowed_origins = config_manager.get_env_var("ALLOWED_ORIGINS", allowed_origins)
    else:
        allowed_origins = os.getenv("ALLOWED_ORIGINS", allowed_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in allowed_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )

    # Add gzip compression middleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # Add Sentry ASGI middleware for automatic request tracking
    if SENTRY_ENABLED:
        try:
            app.add_middleware(SentryAsgiMiddleware)
            log.info("Sentry monitoring enabled for API requests")
        except Exception as e:
            log.warning(f"Failed to add Sentry middleware: {e}")

    # Include routers
    app.include_router(bots.router)
    app.include_router(config.router)
    app.include_router(admin.router)
    app.include_router(personalities.router)
    app.include_router(memories.router)
    app.include_router(system.router)

    # Dashboard and authentication routes
    @app.get("/")
    async def dashboard(request: Request):
        # Check if OAuth is disabled
        disable_oauth = config_manager.get_env_var("DISABLE_OAUTH", "false").lower() == "true"

        if not disable_oauth:
            # Check if user is authenticated
            user = request.session.get("user")
            if not user:
                return RedirectResponse("/login")

        try:
            # Serve modern interface
            return FileResponse("web/modern-index.html")
        except Exception:
            try:
                return FileResponse("web/index.html")
            except Exception:
                return {"message": "GrugThink Management API", "version": __version__, "build": __build_hash__}

    @app.get("/admin")
    async def admin_page(request: Request):
        # Check if OAuth is disabled
        disable_oauth = config_manager.get_env_var("DISABLE_OAUTH", "false").lower() == "true"
        if not disable_oauth:
            # Check if user is authenticated
            user = request.session.get("user")
            if not user:
                return RedirectResponse("/login")
        try:
            return FileResponse("web/admin.html")
        except Exception:
            return {"message": "Admin page not available"}

    @app.get("/admin.html")
    async def serve_admin_html():
        return FileResponse("web/admin.html")

    @app.get("/login")
    async def login(request: Request):
        # Check if OAuth is disabled
        disable_oauth = config_manager.get_env_var("DISABLE_OAUTH", "false").lower() == "true"

        if disable_oauth:
            # If OAuth is disabled, redirect to dashboard
            return RedirectResponse("/")

        # Get Discord OAuth settings from config manager
        client_id = config_manager.get_env_var("DISCORD_CLIENT_ID")
        redirect_uri = config_manager.get_env_var("DISCORD_REDIRECT_URI")

        # Fall back to environment variables
        if not client_id:
            client_id = os.getenv("DISCORD_CLIENT_ID")
        if not redirect_uri:
            redirect_uri = os.getenv("DISCORD_REDIRECT_URI")

        if not client_id or not redirect_uri:
            return {"error": "Discord OAuth not configured"}

        # CSRF protection: a random `state` stored in the session and verified in
        # the callback prevents a forged/replayed OAuth response.
        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state
        params = {
            "client_id": client_id,
            "response_type": "code",
            "scope": "identify",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        url = "https://discord.com/api/oauth2/authorize?" + requests.compat.urlencode(params)
        return RedirectResponse(url)

    @app.get("/callback")
    async def auth_callback(request: Request):
        # Check if OAuth is disabled
        disable_oauth = False
        if config_manager:
            disable_oauth = config_manager.get_env_var("DISABLE_OAUTH", "false").lower() == "true"
        else:
            disable_oauth = os.getenv("DISABLE_OAUTH", "false").lower() == "true"

        if disable_oauth:
            # If OAuth is disabled, redirect to dashboard
            return RedirectResponse("/")

        code = request.query_params.get("code")
        if not code:
            return {"error": "Missing code"}

        # CSRF: the `state` must match the one we stored at /login.
        expected_state = request.session.pop("oauth_state", None)
        received_state = request.query_params.get("state")
        if not expected_state or received_state != expected_state:
            return {"error": "Invalid OAuth state"}

        # Get Discord OAuth settings from config manager
        client_id = config_manager.get_env_var("DISCORD_CLIENT_ID")
        client_secret = config_manager.get_env_var("DISCORD_CLIENT_SECRET")
        redirect_uri = config_manager.get_env_var("DISCORD_REDIRECT_URI")

        # Fall back to environment variables
        if not client_id:
            client_id = os.getenv("DISCORD_CLIENT_ID")
        if not client_secret:
            client_secret = os.getenv("DISCORD_CLIENT_SECRET")
        if not redirect_uri:
            redirect_uri = os.getenv("DISCORD_REDIRECT_URI")

        if not client_id or not client_secret or not redirect_uri:
            return {"error": "Discord OAuth not configured properly"}

        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        # Use a thread + explicit timeout: `requests` is synchronous and would
        # otherwise block the event loop and hang indefinitely against Discord.
        token_res = await asyncio.to_thread(
            requests.post, "https://discord.com/api/oauth2/token", data=data, headers=headers, timeout=10
        )
        token_res.raise_for_status()
        access_token = token_res.json()["access_token"]

        user_res = await asyncio.to_thread(
            requests.get,
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        user_res.raise_for_status()
        user = user_res.json()

        # Get trusted users from config manager
        trusted_str = config_manager.get_env_var("TRUSTED_USER_IDS", "")
        trusted = [t.strip() for t in trusted_str.split(",") if t.strip()]

        if str(user["id"]) not in trusted:
            return RedirectResponse("/", status_code=403)

        request.session["user"] = {"id": str(user["id"]), "username": user["username"]}
        return RedirectResponse("/")

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/")

    @app.get("/api/user")
    async def get_user(request: Request):
        """Get current authenticated user info."""
        # Check if OAuth is disabled
        disable_oauth = config_manager.get_env_var("DISABLE_OAUTH", "false").lower() == "true"

        if disable_oauth:
            # Return dummy user when OAuth is disabled
            return {"id": "admin", "username": "admin"}

        user = request.session.get("user")
        if not user:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})
        return user

    @app.get("/simple-admin")
    async def simple_admin():
        """Simple admin endpoint without dependencies."""
        return {"message": "Simple admin endpoint working"}

    # Debug route list endpoint
    @app.get("/api/debug/routes")
    async def list_all_routes():
        routes = []
        for route in app.routes:
            if hasattr(route, "path") and hasattr(route, "methods"):
                routes.append({"path": route.path, "methods": list(route.methods) if route.methods else []})
        return {"routes": routes}

    # Setup static file serving for web dashboard with caching
    try:

        class CachedStaticFiles(StaticFiles):
            """Custom StaticFiles with better caching headers."""

            def file_response(self, full_path: str, stat_result: os.stat_result, scope: dict, status_code: int = 200):
                response = super().file_response(full_path, stat_result, scope, status_code)
                path_obj = Path(full_path)
                # Add cache headers for better performance
                # NO CACHE for JS/CSS during development to prevent stale code issues
                if path_obj.suffix in [".js", ".css"]:
                    response.headers["Cache-Control"] = "no-cache, must-revalidate"  # Always check server
                elif path_obj.suffix in [".png", ".jpg", ".ico"]:
                    response.headers["Cache-Control"] = "public, max-age=86400"  # 1 day for images
                elif path_obj.suffix in [".html"]:
                    response.headers["Cache-Control"] = "no-cache, must-revalidate"  # Always check for HTML

                # Add compression hint
                response.headers["Vary"] = "Accept-Encoding"

                return response

        app.mount("/static", CachedStaticFiles(directory="web/static"), name="static")

    except Exception:
        log.warning("Static files directory not found, web dashboard may not work")

    log.info("FastAPI application created successfully")
    return app


async def run_server(bot_manager: BotManager, config_manager: ConfigManager):
    """Run the API server with bot monitoring."""
    # Create FastAPI app
    app = create_app(bot_manager, config_manager)

    # Start bot monitoring
    monitoring_task = asyncio.create_task(bot_manager.monitor_bots())

    try:
        # Run the server. uvicorn.Server.serve() installs its own SIGINT/SIGTERM
        # handling (via capture_signals()) for graceful shutdown, so no custom
        # signal registration is needed here.
        host = os.getenv("API_HOST", "0.0.0.0")
        port = int(os.getenv("API_PORT", "8080"))
        config = uvicorn.Config(app, host=host, port=port)
        server = uvicorn.Server(config)
        await server.serve()

    finally:
        # Cleanup
        monitoring_task.cancel()
        log.info("Stopping all bots...")
        await bot_manager.stop_all_bots()
        config_manager.stop()
        log.info("Graceful shutdown complete.")


async def main():
    """Main entry point for the API server."""
    # Setup logging
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    setup_logging(log_level)

    # Initialize managers
    config_manager = ConfigManager()
    bot_manager = BotManager(config_manager=config_manager)

    # Run server
    await run_server(bot_manager, config_manager)


if __name__ == "__main__":
    asyncio.run(main())
