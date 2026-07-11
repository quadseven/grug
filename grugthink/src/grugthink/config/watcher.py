#!/usr/bin/env python3
"""
Configuration File Watcher

Handles hot-reloading of configuration changes with watchdog.
"""

import os

from ..grug_structured_logger import get_logger

log = get_logger(__name__)

# Optional dependency for file watching
try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _WATCHDOG_AVAILABLE = True
except ImportError:
    FileSystemEventHandler = None
    Observer = None
    _WATCHDOG_AVAILABLE = False


if _WATCHDOG_AVAILABLE:

    class ConfigChangeHandler(FileSystemEventHandler):
        """Handles configuration file changes."""

        def __init__(self, config_manager):
            self.config_manager = config_manager

        def on_modified(self, event):
            if not event.is_directory and os.path.abspath(event.src_path) == os.path.abspath(
                self.config_manager.config_file
            ):
                self.config_manager._reload_config()
else:

    class ConfigChangeHandler:
        """Stub class when watchdog is not available."""

        def __init__(self, config_manager):
            pass


def start_watching(config_manager):
    """Start watching configuration file for changes."""
    if not _WATCHDOG_AVAILABLE or config_manager.observer is None:
        log.info("File watching disabled (watchdog not available)")
        return

    try:
        config_dir = os.path.dirname(os.path.abspath(config_manager.config_file))
        config_manager.observer.schedule(config_manager.handler, config_dir, recursive=False)
        config_manager.observer.start()

        log.info("Started configuration file watcher", extra={"directory": config_dir})

    except Exception as e:
        log.error("Failed to start file watcher", extra={"error": str(e)})


def stop_watching(config_manager):
    """Stop the configuration file watcher."""
    if config_manager.observer and hasattr(config_manager.observer, "is_alive") and config_manager.observer.is_alive():
        config_manager.observer.stop()
        config_manager.observer.join()
        log.info("Configuration file watcher stopped")


def is_watchdog_available() -> bool:
    """Check if watchdog is available."""
    return _WATCHDOG_AVAILABLE


def create_observer_and_handler(config_manager):
    """Create observer and handler for file watching."""
    if _WATCHDOG_AVAILABLE:
        observer = Observer()
        handler = ConfigChangeHandler(config_manager)
    else:
        observer = None
        handler = ConfigChangeHandler(config_manager)

    return observer, handler
