"""Consume a `FallbackResult` and dispatch it to a per-persona handler.

Guarantees the consumer cares about: it NEVER raises back into the SQS consumer
(a poison result must not retry-storm the queue), and it skips unknown personas.
Idempotency on redelivery is the handler's responsibility (it dedups on
`request_id`). Generalized from grug's `handle_fallback_result`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from .schema import FallbackResult

log = logging.getLogger("spark_cave.results")


def handle_result(
    body: str,
    *,
    handlers: Mapping[str, Callable[[FallbackResult], None]],
) -> bool:
    """Parse one result message and dispatch to `handlers[persona]`. Returns
    True if a handler ran, else False. Never raises -- malformed bodies, unknown
    personas, and handler exceptions are logged and swallowed so the consumer's
    delete-on-success loop stays healthy."""
    try:
        result = FallbackResult.from_json(body)
    except Exception as e:
        log.warning("spark_cave: dropping unparseable result: %s", e)
        return False

    handler = handlers.get(result.persona)
    if handler is None:
        log.warning(
            "spark_cave: no handler for persona %r (request %s)",
            result.persona,
            result.request_id,
        )
        return False

    try:
        handler(result)
    except Exception as e:
        log.warning(
            "spark_cave: handler for %r raised on request %s: %s",
            result.persona,
            result.request_id,
            e,
        )
        return False
    return True
