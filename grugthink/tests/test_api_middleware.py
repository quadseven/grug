"""Response cache middleware (#1691)."""

import pytest

from src.grugthink.api import middleware


@pytest.fixture(autouse=True)
def _clean_cache():
    middleware.clear_cache()
    yield
    middleware.clear_cache()


@pytest.mark.asyncio
async def test_cached_response_is_reused_within_ttl():
    calls = []

    @middleware.cache_response(ttl=60)
    async def handler(x):
        calls.append(x)
        return {"x": x}

    assert await handler(1) == {"x": 1}
    assert await handler(1) == {"x": 1}
    assert calls == [1]
    assert await handler(2) == {"x": 2}
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_expired_entry_is_recomputed(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(middleware.time, "time", lambda: now[0])
    calls = []

    @middleware.cache_response(ttl=10)
    async def handler():
        calls.append(1)
        return len(calls)

    assert await handler() == 1
    now[0] += 11
    assert await handler() == 2


def test_eviction_drops_expired_then_oldest(monkeypatch):
    monkeypatch.setattr(middleware.time, "time", lambda: 1000.0)
    middleware.API_CACHE["expired"] = ("e", 900.0)
    for i in range(5):
        middleware.API_CACHE[f"k{i}"] = (i, 990.0 + i)
    middleware._evict_stale_entries(ttl=30, max_size=3)
    assert "expired" not in middleware.API_CACHE
    assert sorted(middleware.API_CACHE) == ["k2", "k3", "k4"]


def test_clear_cache_empties_it():
    middleware.API_CACHE["a"] = (1, 0.0)
    middleware.clear_cache()
    assert middleware.API_CACHE == {}
