#!/usr/bin/env python3
"""
GrugThink Multi-Bot Container Entry Point

Main entry point for the multi-bot container system. Handles process isolation,
graceful shutdown, and coordination between the API server and bot instances.
"""

import argparse
import asyncio
import os
import signal
import sys
from typing import Any, Dict

from .api.server import create_app
from .bot_manager import BotManager
from .config.manager import ConfigManager
from .grug_structured_logger import get_logger

# Initialize Sentry monitoring if configured (after imports to avoid E402)
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            # Set traces_sample_rate to 1.0 to capture 100% of transactions for performance monitoring.
            # Adjust this value in production to reduce volume
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            # Set profiles_sample_rate to 1.0 to profile 100% of sampled transactions.
            # Adjust this value in production to reduce volume
            profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
            # Add request headers and user data for better error context
            send_default_pii=True,
            # Set environment tag
            environment=os.getenv("GRUGBOT_VARIANT", "production"),
            # Enable integrations
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                LoggingIntegration(
                    level=None,  # Capture all log levels
                    event_level=None,  # Don't send logs as events by default
                ),
            ],
            # Enable release tracking
            release=f"grugthink@{os.getenv('VERSION', 'unknown')}",
            # Add custom tags
            before_send=lambda event, hint: _enrich_sentry_event(event, hint),
        )
    except ImportError:
        pass  # Sentry will be unavailable but app can continue


def _enrich_sentry_event(event, hint):
    """Add custom context to Sentry events."""
    # Add bot instance information
    event.setdefault("tags", {})
    event["tags"]["component"] = "grugthink"
    event["tags"]["bot_count"] = str(len(os.listdir("/data")) if os.path.exists("/data") else 0)

    # Add exception context if available
    if "exc_info" in hint:
        exc_type, exc_value, exc_tb = hint["exc_info"]
        event.setdefault("extra", {})
        event["extra"]["exception_type"] = exc_type.__name__ if exc_type else "Unknown"

    return event


log = get_logger(__name__)

# Global reference for cross-bot communication
_global_bot_manager = None


def get_bot_manager():
    """Get the global bot manager instance for cross-bot operations."""
    return _global_bot_manager


class GrugThinkContainer:
    """Main container orchestrator for the multi-bot system."""

    def __init__(self):
        global _global_bot_manager
        # Allow config path override for local development
        config_path = os.getenv("GRUGTHINK_CONFIG_PATH", "/data/grugthink_config.yaml")
        self.config_manager = ConfigManager(config_path)
        self.bot_manager = BotManager(config_manager=self.config_manager)
        # Create the FastAPI app using the function-based approach
        self.app = create_app(self.bot_manager, self.config_manager)

        # Set global reference for cross-bot communication
        _global_bot_manager = self.bot_manager

        self.running = False
        self.tasks = []

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        log.info("GrugThink Container initialized")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        log.info("Received shutdown signal", extra={"signal": signum})
        asyncio.create_task(self.shutdown())

    async def start(self, start_bots: bool = True, api_port: int = 8080):
        """Start the container system."""
        try:
            self.running = True
            log.info("Starting GrugThink Container", extra={"start_bots": start_bots, "api_port": api_port})

            # Start configuration change monitoring
            self.config_manager.add_change_callback(self._on_config_change)

            # Start bot monitoring task
            monitoring_task = asyncio.create_task(self.bot_manager.monitor_bots())
            self.tasks.append(monitoring_task)

            # Start all configured bots if requested
            if start_bots:
                log.info("About to call _start_configured_bots")
                await self._start_configured_bots()
            else:
                log.info("Skipping bot auto-start (start_bots=False)")

            # Start API server in background
            api_task = asyncio.create_task(self._run_api_server(api_port))
            self.tasks.append(api_task)

            log.info(
                "GrugThink Container started successfully", extra={"api_port": api_port, "auto_start_bots": start_bots}
            )

            # Keep running until shutdown
            while self.running:
                await asyncio.sleep(1)

        except Exception as e:
            log.error("Error starting container", extra={"error": str(e)})
            await self.shutdown()
            raise

    async def _start_configured_bots(self):
        """Start all bots marked for auto-start in configuration."""
        log.info("_start_configured_bots called - checking for bots to auto-start")
        try:
            bots = self.bot_manager.list_bots()
            log.info(
                "Found bots in configuration",
                extra={"bot_count": len(bots), "bot_ids": [b.get("bot_id") for b in bots]},
            )
            auto_start_bots = []

            for bot in bots:
                # Check auto_start flag first, then fall back to enabled status
                auto_start_flag = self.config_manager.get_config(f"bot_configs.{bot['bot_id']}.auto_start")
                bot_enabled = self.config_manager.get_config(f"bot_configs.{bot['bot_id']}.enabled")

                log.info(
                    "Checking bot for auto-start",
                    extra={"bot_id": bot.get("bot_id"), "auto_start_flag": auto_start_flag, "bot_enabled": bot_enabled},
                )

                # Priority: explicit auto_start flag, then enabled status
                should_start = auto_start_flag is True or (auto_start_flag is None and bot_enabled is True)

                if should_start:
                    auto_start_bots.append(bot)
                    log.info("Bot marked for auto-start", extra={"bot_id": bot.get("bot_id")})

            if auto_start_bots:
                log.info("Starting configured bots", extra={"count": len(auto_start_bots)})

                # Start bots concurrently for better performance
                start_tasks = []
                for i, bot in enumerate(auto_start_bots):
                    # Stagger starts by 2 seconds instead of 5
                    start_tasks.append(asyncio.create_task(self._start_bot_with_delay(bot["bot_id"], i * 2)))

                # Wait for all bots to start
                if start_tasks:
                    await asyncio.gather(*start_tasks, return_exceptions=True)
            else:
                log.info("No bots configured for auto-start")

        except Exception as e:
            log.error("Error starting configured bots", extra={"error": str(e)})

    async def _start_bot_with_delay(self, bot_id: str, delay: int):
        """Start a bot with a specified delay."""
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self.bot_manager.start_bot(bot_id)
        except Exception as e:
            log.error("Failed to start bot", extra={"bot_id": bot_id, "error": str(e)})

    async def _run_api_server(self, port: int):
        """Run the API server with performance optimizations."""
        import uvicorn

        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            # Performance optimizations
            workers=1,  # Single worker for container
            loop="asyncio",
            http="h11",
            access_log=False,  # Disable access logs for performance
            server_header=False,  # Remove server header
            date_header=False,  # Remove date header
        )
        server = uvicorn.Server(config)
        await server.serve()

    def _on_config_change(
        self, old_config: Dict[str, Any], new_config: Dict[str, Any], old_env: Dict[str, str], new_env: Dict[str, str]
    ):
        """Handle configuration changes."""
        log.info("Configuration changed", extra={"config_keys": list(new_config.keys()), "env_vars": len(new_env)})

        # TODO: Handle specific configuration changes
        # - Restart bots if their config changed
        # - Update API keys
        # - Reload templates

    async def shutdown(self):
        """Gracefully shutdown the container."""
        if not self.running:
            return

        log.info("Shutting down GrugThink Container")
        self.running = False

        try:
            # Stop all bots
            await self.bot_manager.stop_all_bots()

            # Cancel all background tasks
            for task in self.tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            # Stop configuration manager
            self.config_manager.stop()

            log.info("GrugThink Container shutdown complete")

        except Exception as e:
            log.error("Error during shutdown", extra={"error": str(e)})


async def main():
    """Main entry point."""
    # Load .env file if it exists
    try:
        from dotenv import load_dotenv

        if os.path.exists(".env"):
            load_dotenv(".env")
            log.info("Loaded environment variables from .env file")
    except ImportError:
        log.warning("python-dotenv not installed, .env file not loaded")

    # Set multi-bot mode flag to skip single-bot config validation
    os.environ["GRUGTHINK_MULTIBOT_MODE"] = "true"

    parser = argparse.ArgumentParser(description="GrugThink Multi-Bot Container")
    parser.add_argument("--no-auto-start", action="store_true", help="Don't automatically start configured bots")
    parser.add_argument("--api-port", type=int, default=8080, help="Port for the management API (default: 8080)")
    parser.add_argument("--create-demo", action="store_true", help="Create demo bot configurations")

    args = parser.parse_args()

    # Create demo configurations if requested
    if args.create_demo:
        await create_demo_configuration()
        return

    # Start the container
    container = GrugThinkContainer()

    try:
        await container.start(start_bots=not args.no_auto_start, api_port=args.api_port)
    except KeyboardInterrupt:
        log.info("Received keyboard interrupt")
    except Exception as e:
        log.error("Container error", extra={"error": str(e)})
        sys.exit(1)
    finally:
        await container.shutdown()


async def create_demo_configuration():
    """Create demonstration bot configurations."""
    log.info("Creating demo configuration")

    config_manager = ConfigManager("grugthink_config.yaml")
    bot_manager = BotManager(config_manager=config_manager)

    try:
        # Check if we have any Discord tokens configured
        tokens = config_manager.get_discord_tokens()
        if not tokens:
            log.warning("No Discord tokens configured. Add tokens via the web interface or config file.")
            return

        available_token = config_manager.get_available_discord_token()
        if not available_token:
            log.warning("No available Discord tokens found")
            return

        demo_bots = [
            {"name": "Pure Grug Bot", "template": "pure_grug", "description": "Caveman personality only"},
            {"name": "Pure Big Rob Bot", "template": "pure_big_rob", "description": "norf FC lad personality only"},
            {"name": "Evolution Bot", "template": "evolution_bot", "description": "Adaptive personality that evolves"},
        ]

        created_bots = []
        for i, bot_config in enumerate(demo_bots):
            # Use the same token for demo - in practice you'd use different tokens
            bot_id = bot_manager.create_bot(
                name=bot_config["name"],
                discord_token=available_token,  # Same token for demo
                # In production, you'd want separate tokens per bot
            )
            created_bots.append((bot_id, bot_config["name"]))

            log.info(
                "Created demo bot",
                extra={"bot_id": bot_id, "bot_name": bot_config["name"], "template": bot_config["template"]},
            )

        log.info(
            "Demo configuration created",
            extra={"bot_count": len(created_bots), "bots": [name for _, name in created_bots]},
        )

        print("\n🎉 Demo configuration created!")
        print(f"Created {len(created_bots)} demo bots:")
        for bot_id, name in created_bots:
            print(f"  - {name} (ID: {bot_id})")
        print("\nStart the container with: python main.py")
        print("Then visit http://localhost:8080 to manage your bots!")

    except Exception as e:
        log.error("Failed to create demo configuration", extra={"error": str(e)})
        print(f"❌ Failed to create demo configuration: {e}")


if __name__ == "__main__":
    # Set up proper event loop policy for Windows
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(main())
