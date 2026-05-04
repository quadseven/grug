"""Regression tests for #50 — InMemoryTokenCache.invalidate."""

from __future__ import annotations

from ports.token_cache import InMemoryTokenCache


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
