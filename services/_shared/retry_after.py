"""Parse a Retry-After header, in delta-seconds or HTTP-date form (RFC 9110)."""
from __future__ import annotations

import math
import time
from email.utils import parsedate_to_datetime

import httpx


def parse_retry_after(resp: httpx.Response, now: float | None = None) -> float | None:
    """Seconds to wait, or None when absent, unparseable, non-finite or not positive.

    A date in the past is unusable. `now` is epoch seconds, default time.time().
    """
    raw = (resp.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(raw).timestamp() - (
                time.time() if now is None else now
            )
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds
