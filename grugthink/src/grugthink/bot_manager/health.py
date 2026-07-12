#!/usr/bin/env python3
"""
Bot Health Monitoring

Monitors bot health and handles automatic restarts with backoff logic.
"""

import asyncio
import os
import time

from ..grug_structured_logger import get_logger

log = get_logger(__name__)


async def monitor_bots(bot_manager):
    """Monitor bot health and update heartbeats with comprehensive health checking."""
    while bot_manager.running:
        try:
            for bot_id, instance in bot_manager.bots.items():
                await check_bot_health(bot_manager, bot_id, instance)

            # Use configurable health check interval
            health_check_interval = float(os.getenv("HEALTH_CHECK_INTERVAL", "30"))
            await asyncio.sleep(health_check_interval)

        except Exception as e:
            log.error("Error in bot monitoring", extra={"error": str(e)})
            await asyncio.sleep(60)  # Wait longer on error


async def check_bot_health(bot_manager, bot_id: str, instance):
    """Comprehensive health check for a single bot instance."""
    if not instance.config.enabled:
        return  # Skip disabled bots

    current_time = time.time()

    # Get configurable thresholds
    heartbeat_timeout = float(os.getenv("BOT_HEARTBEAT_TIMEOUT", "300"))
    high_latency_threshold = float(os.getenv("BOT_HIGH_LATENCY_THRESHOLD", "5.0"))

    # Check if bot should be running but isn't
    if instance.runtime_status in ["stopped", "error"] and (
        (instance.config.auto_start is True) or (instance.config.auto_start is None and instance.config.enabled)
    ):
        # Bot should be running but isn't - attempt restart
        await attempt_bot_restart(bot_manager, bot_id, instance, "Bot should be running but is stopped")
        return

    # Check running bots for health issues
    if instance.runtime_status == "running":
        health_issues = []

        # Check 1: Discord client health
        if not instance.client or not instance.client.is_ready():
            health_issues.append("Discord client not ready")

        # Check 2: Heartbeat timeout
        if instance.last_heartbeat and (current_time - instance.last_heartbeat) > heartbeat_timeout:
            health_issues.append(f"Heartbeat timeout ({int(current_time - instance.last_heartbeat)}s)")

        # Check 3: Task is dead/cancelled
        if instance.task and instance.task.done():
            exception = instance.task.exception()
            if exception:
                health_issues.append(f"Task died with exception: {exception}")
            else:
                health_issues.append("Task completed unexpectedly")

        # Check 4: Client latency too high
        if instance.client and instance.client.is_ready():
            latency = instance.client.latency
            if latency > high_latency_threshold:
                health_issues.append(f"High latency: {latency:.2f}s (threshold: {high_latency_threshold}s)")

        # If health issues found, attempt restart
        if health_issues:
            await attempt_bot_restart(bot_manager, bot_id, instance, f"Health issues: {', '.join(health_issues)}")
        else:
            # Bot is healthy - update heartbeat and reset failure count
            instance.last_heartbeat = current_time
            instance.consecutive_failures = 0


async def attempt_bot_restart(bot_manager, bot_id: str, instance, reason: str):
    """Attempt to restart a bot with backoff and failure limits."""
    current_time = time.time()

    # Get configurable limits
    rate_limit = float(os.getenv("BOT_RESTART_RATE_LIMIT", "120"))
    max_failures = int(os.getenv("BOT_MAX_CONSECUTIVE_FAILURES", "5"))
    max_backoff = float(os.getenv("BOT_RESTART_BACKOFF_MAX", "300"))

    # Check restart rate limiting
    if instance.last_restart_attempt and (current_time - instance.last_restart_attempt) < rate_limit:
        return

    # Check failure limits
    if instance.consecutive_failures >= max_failures:
        instance.logger.error(
            "Bot has failed too many times, giving up on restarts",
            extra={
                "failures": instance.consecutive_failures,
                "restart_count": instance.restart_count,
                "reason": reason,
                "max_failures": max_failures,
            },
        )
        instance.runtime_status = "error"
        return

    # Exponential backoff based on failure count
    backoff_delay = min(60 * (2**instance.consecutive_failures), max_backoff)
    if instance.last_restart_attempt and (current_time - instance.last_restart_attempt) < backoff_delay:
        return

    instance.last_restart_attempt = current_time
    instance.restart_count += 1

    instance.logger.warning(
        "Attempting bot restart",
        extra={
            "reason": reason,
            "failures": instance.consecutive_failures,
            "restart_count": instance.restart_count,
            "backoff_delay": backoff_delay,
        },
    )

    try:
        # Import lifecycle methods
        from .lifecycle import start_bot, stop_bot

        # Stop the bot first
        await stop_bot(bot_manager, bot_id)

        # Wait a moment for cleanup
        await asyncio.sleep(5)

        # Start the bot again
        success = await start_bot(bot_manager, bot_id)

        if success:
            instance.logger.info("Bot restart successful", extra={"restart_count": instance.restart_count})
        else:
            instance.consecutive_failures += 1
            instance.logger.error(
                "Bot restart failed",
                extra={"failures": instance.consecutive_failures, "restart_count": instance.restart_count},
            )

    except Exception as e:
        instance.consecutive_failures += 1
        instance.logger.error(
            "Exception during bot restart",
            extra={
                "error": str(e),
                "failures": instance.consecutive_failures,
                "restart_count": instance.restart_count,
            },
        )
