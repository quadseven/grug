"""Regression tests for #50 — InMemoryTokenCache.invalidate +
type-design-analyzer constructor invariants."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from ports.token_cache import CachedToken, InMemoryTokenCache


def test_invalidate_drops_entry():
    c = InMemoryTokenCache()
    c.put("k", "v", ttl_seconds=300)
    assert c.get("k") is not None
    c.invalidate("k")
    assert c.get("k") is None


def test_invalidate_unknown_key_no_op():
    c = InMemoryTokenCache()
    c.invalidate("never-set")  # must not raise


def test_get_after_re_put_returns_new_value():
    c = InMemoryTokenCache()
    c.put("k", "v1", ttl_seconds=300)
    c.invalidate("k")
    c.put("k", "v2", ttl_seconds=300)
    assert c.get("k").value == "v2"


def test_cached_token_rejects_empty_value():
    """type-design-analyzer: empty token value would mask 'no token'
    as 'fresh empty token'. Reject at construction."""
    with pytest.raises(ValueError, match="non-empty"):
        CachedToken(value="", expires_at_unix=9999999999.0)


def test_cached_token_rejects_nonpositive_expires():
    """type-design-analyzer: expires_at_unix <= 0 would always be expired."""
    with pytest.raises(ValueError, match="must be > 0"):
        CachedToken(value="tok", expires_at_unix=0)
    with pytest.raises(ValueError, match="must be > 0"):
        CachedToken(value="tok", expires_at_unix=-1)


def test_cached_token_frozen():
    """frozen=True so callers can't mutate cached entries from under us."""
    t = CachedToken(value="v", expires_at_unix=9999999999.0)
    with pytest.raises(FrozenInstanceError):
        t.value = "evil"  # type: ignore[misc]
