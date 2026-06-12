"""User store - Postgres-backed since the #354 store swap (api-only).

FACADE over the Postgres implementation; the canonical UserWithTokens
construction site + decrypt boundary now live in pg_user_store.py
(spec 0008's attester points there).
"""

from adapters.pg_user_store import *  # noqa: F401,F403
