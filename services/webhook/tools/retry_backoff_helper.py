"""Bounded retry helper for the webhook's synchronous dispatch path.

Runs on the dispatcher WORKER THREAD (asyncio.to_thread in main.py), never
on the event loop - blocking sleep here is correct by design, mirroring
publish_check.py's transient retry (#697).
"""

from __future__ import annotations

import time


def retry_with_backoff(fn, attempts: int = 3, base_delay: float = 0.5):
    """Call `fn` with exponential backoff between attempts."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except OSError as e:
            last_error = e
            time.sleep(base_delay * (2 ** attempt))
    if last_error is not None:
        raise last_error
    return None
