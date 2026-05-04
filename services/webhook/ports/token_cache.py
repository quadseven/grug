"""TokenCache port — swappable cache for App JWTs + install tokens.

v1 = InMemoryTokenCache (module-scope dict in warm Lambda container).
v2 = DdbTokenCache (cross-container shared) — same Protocol, swap via env.

Per PRD #21 Q17: cold start re-signs JWT (~10ms) + re-fetches install
token (~150ms) per warm container. Acceptable for v1; revisit if cold
start dominates p99.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


@dataclass
class CachedToken:
    value: str
    expires_at_unix: float

    def is_fresh(self, skew_seconds: float = 30) -> bool:
        return time.time() < self.expires_at_unix - skew_seconds


class TokenCache(Protocol):
    """Get/put/invalidate for short-lived auth tokens.

    Callers MUST invalidate on 401 — GitHub can revoke tokens before
    TTL expiry (App reinstall, perm change, secret rotation). Without
    invalidate, a warm Lambda keeps reusing the bad token until TTL.
    Codex post-review #50.
    """

    def get(self, key: str) -> CachedToken | None: ...

    def put(self, key: str, token: str, ttl_seconds: float) -> None: ...

    def invalidate(self, key: str) -> None: ...


class InMemoryTokenCache:
    """Module-scope dict cache. Lifetime = warm Lambda container."""

    def __init__(self) -> None:
        self._store: dict[str, CachedToken] = {}

    def get(self, key: str) -> CachedToken | None:
        cached = self._store.get(key)
        if cached and cached.is_fresh():
            return cached
        return None

    def put(self, key: str, token: str, ttl_seconds: float) -> None:
        self._store[key] = CachedToken(
            value=token, expires_at_unix=time.time() + ttl_seconds,
        )

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)
