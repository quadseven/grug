"""Middleware utilities for API server."""

import time
from functools import wraps
from typing import Dict

# Simple cache for API responses
API_CACHE: Dict[str, tuple] = {}  # key: (data, timestamp)
CACHE_TTL = 30  # seconds


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

            # Clean old cache entries periodically
            if len(API_CACHE) > 100:
                API_CACHE.clear()  # Simple cleanup

            return result

        return wrapper

    return decorator


def clear_cache():
    """Clear all cached responses."""
    API_CACHE.clear()
