"""Parse a `Retry-After` header (RFC 9110 section 10.2.3): delta-seconds or HTTP-date."""

from __future__ import annotations

import math
import time
from email.utils import parsedate_to_datetime


def parse_retry_after(raw: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from `now`, or None when absent, unparseable,
    non-finite, zero/negative, or an HTTP-date already in the past."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            return None
        seconds = when.timestamp() - (time.time() if now is None else now)
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds
