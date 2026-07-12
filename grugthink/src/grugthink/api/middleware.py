"""Middleware utilities for API server."""

import time
from functools import wraps
from typing import Dict

# Simple cache for API responses
API_CACHE: Dict[str, tuple] = {}  # key: (data, timestamp)
CACHE_TTL = 30  # seconds


def _evict_stale_entries(ttl: int, max_size: int = 100) -> None:
    """Drop expired entries, then evict the oldest ones until under max_size."""
    now = time.time()
    expired = [key for key, (_, timestamp) in API_CACHE.items() if now - timestamp >= ttl]
    for key in expired:
        del API_CACHE[key]

    if len(API_CACHE) > max_size:
        oldest_first = sorted(API_CACHE.items(), key=lambda item: item[1][1])
        for key, _ in oldest_first[: len(API_CACHE) - max_size]:
            del API_CACHE[key]


def cache_response(ttl: int = CACHE_TTL):
    """Decorator to cache API responses for better performance."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Create cache key from function name and args
            cache_key = f"{func.__name__}:{hash(str(args) + str(kwargs))}"

            # Check cache
            if cache_key in API_CACHE:
                data, timestamp = API_CACHE[cache_key]
                if time.time() - timestamp < ttl:
                    return data

            # Execute function and cache result
            result = await func(*args, **kwargs)
            API_CACHE[cache_key] = (result, time.time())

            # Bound cache size without discarding still-warm entries: drop
            # expired entries first, then evict the oldest until back under
            # the limit.
            if len(API_CACHE) > 100:
                _evict_stale_entries(ttl)

            return result

        return wrapper

    return decorator


def clear_cache():
    """Clear all cached responses."""
    API_CACHE.clear()
