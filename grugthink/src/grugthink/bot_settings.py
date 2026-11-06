"""Per-bot settings for multi-bot mode (grug#1667).

Each bot's environment (trusted ids, Ollama URLs and models, API keys,
personality) lives in a context variable instead of process-global
``os.environ``. ``bot_manager.lifecycle.start_bot`` activates a bot's settings
while it builds the bot, and the Discord client task it creates copies that
context, so every handler the bot runs reads its own values.

``getenv`` reads the active bot's value first and falls back to the process
environment, which is the only source in single-bot mode.

Executor threads do not inherit a context: hand work to a thread with
``asyncio.to_thread`` (which copies it), not ``loop.run_in_executor``.
"""

from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar
from types import MappingProxyType
from typing import Iterator, Mapping, Optional

_current: ContextVar[Optional[Mapping[str, str]]] = ContextVar("grugthink_bot_settings", default=None)


def getenv(name: str, default: Optional[str] = None) -> Optional[str]:
    """Return the active bot's setting, else the process environment's."""
    settings = _current.get()
    if settings is not None and name in settings:
        return settings[name]
    return os.getenv(name, default)


def current() -> Optional[Mapping[str, str]]:
    """The active bot's settings, or None outside any bot (single-bot mode)."""
    return _current.get()


@contextlib.contextmanager
def activate(settings: Mapping[str, str]) -> Iterator[Mapping[str, str]]:
    """Make ``settings`` the active bot's for the current context.

    Tasks created inside the block copy the context and keep these settings
    after the block exits; the caller's context is restored on exit.
    """
    frozen = MappingProxyType(dict(settings))
    token = _current.set(frozen)
    try:
        yield frozen
    finally:
        _current.reset(token)
