import json
import logging
import sys
from datetime import datetime, timezone

from .grug_structured_logger import StructuredLogger

RECENT_LOGS = []


class _InMemoryLogHandler(logging.Handler):
    """Simple logging handler that keeps recent logs in memory."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recent_logs = RECENT_LOGS

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()

        # Try to parse JSON message from StructuredLogger
        structured_data = None
        try:
            structured_data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            # Not a JSON message, use as-is
            pass

        # Extract message and other data
        if structured_data and isinstance(structured_data, dict):
            log_entry = structured_data
            actual_message = structured_data.get("message", message)
            bot_id = structured_data.get("bot_id")
        else:
            log_entry = {"message": message}
            actual_message = message
            bot_id = None

        log_entry.update(
            {
                "level": record.levelname.lower(),
                "message": actual_message,
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "logger": record.name,
            }
        )

        # Extract bot_id from various sources
        if bot_id:
            log_entry["bot_id"] = bot_id
        elif hasattr(record, "bot_id"):
            log_entry["bot_id"] = record.bot_id
        elif "extra" in record.__dict__ and isinstance(record.extra, dict):
            if "bot_id" in record.extra:
                log_entry["bot_id"] = record.extra["bot_id"]

        self.recent_logs.append(log_entry)
        if len(self.recent_logs) > 2000:  # Increased buffer for multiple bots
            self.recent_logs.pop(0)

    def get_recent_logs(self):
        return sorted(self.recent_logs, key=lambda x: x.get("timestamp"))


_in_memory_handler = _InMemoryLogHandler()


def setup_logging(log_level="INFO"):
    """Set up logging for the application."""
    # Get the root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove any existing handlers to avoid duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create a stream handler to output to console
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(log_level)

    # Create a formatter
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    stream_handler.setFormatter(formatter)

    # Add the handler to the root logger
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(_in_memory_handler)

    # Get a logger for this module
    log = logging.getLogger(__name__)
    log.info(f"Logging configured with level {log_level}")


def get_logger(name, **kwargs):
    """
    Get a logger instance that will produce structured JSON logs.

    :param name: The name of the logger.
    :param kwargs: Key-value pairs to be included in every log message.
    """
    logger = logging.getLogger(name)
    return StructuredLogger(logger, kwargs)
