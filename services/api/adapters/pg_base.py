# MIRRORED — sibling at services/webhook/adapters/pg_base.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Shared Postgres plumbing for the single-table store port (#354).

Replaces DynamoDB's single-table (PK/SK + attrs + GSI1 + ttl) with an
EXACT-parity Postgres table:

    grug_kv(pk, sk, data jsonb, gsi1pk, gsi1sk, ttl)

Parity rules (the migration's correctness contract):
- Items round-trip as {"PK": pk, "SK": sk, **attrs}; attrs live in
  `data` jsonb. GSI1PK/GSI1SK and ttl are LIFTED into columns (indexed)
  AND kept in `data` when present, mirroring DDB where they are
  ordinary attributes that the index/TTL machinery reads.
- Binary attrs (the KMS-encrypted oauth_*_blob values) cannot ride
  jsonb raw; they are encoded as {"__b64__": "<base64>"} sentinels by
  the codec below and decoded back to `bytes` on read - callers see
  bytes exactly as boto3 returned them (modulo DDB's Binary wrapper,
  which callers already unwrap defensively).
- DDB TTL deletes rows lazily; Postgres has no reaper, so EVERY read
  filters `ttl IS NULL OR ttl > now()` (the `_TTL_LIVE` predicate) and
  writers that must atomically take over an expired row (claim_delivery)
  encode that in their ON CONFLICT clause. An opportunistic purge runs
  at most once per process-hour to keep the table bounded.

Connection: GRUG_DATABASE_URL (postgresql://...). Lazy pool init with
double-checked locking - same rationale as the DDB _LazyTable (env vars
are monkeypatched after import in tests; eager init would break them).
Schema bootstrap is idempotent (CREATE TABLE IF NOT EXISTS) and runs on
first pool acquisition; concurrent bootstrappers are safe (IF NOT EXISTS
+ advisory lock).
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger("grug.adapters.pg_base")

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()
_last_purge: float = 0.0
_PURGE_INTERVAL_SECONDS = 3600

# One arbitrary-but-fixed key for the schema-bootstrap advisory lock.
_BOOTSTRAP_LOCK_KEY = 0x6772_7567  # "grug"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grug_kv (
    pk      text NOT NULL,
    sk      text NOT NULL,
    data    jsonb NOT NULL DEFAULT '{}'::jsonb,
    gsi1pk  text,
    gsi1sk  text,
    ttl     bigint,
    PRIMARY KEY (pk, sk)
);
CREATE INDEX IF NOT EXISTS grug_kv_gsi1
    ON grug_kv (gsi1pk, gsi1sk) WHERE gsi1pk IS NOT NULL;
CREATE INDEX IF NOT EXISTS grug_kv_ttl
    ON grug_kv (ttl) WHERE ttl IS NOT NULL;
"""

# SQL fragment: row is live (not TTL-expired). Interpolated as a
# constant fragment, never with user input.
TTL_LIVE = "(ttl IS NULL OR ttl > EXTRACT(EPOCH FROM now()))"


def _database_url() -> str:
    url = os.environ.get("GRUG_DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "GRUG_DATABASE_URL is not set - the Postgres store cannot start. "
            "(k8s injects it from the deployment secret; tests set it from "
            "the testcontainer.)"
        )
    return url


def get_pool() -> ConnectionPool:
    """Lazy, thread-safe pool. Bootstraps the schema on first creation."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                pool = ConnectionPool(
                    _database_url(),
                    min_size=1,
                    max_size=int(os.environ.get("GRUG_PG_POOL_MAX", "5")),
                    open=True,
                )
                with pool.connection() as conn:
                    # Advisory lock so N replicas bootstrapping at once
                    # don't race the CREATEs (IF NOT EXISTS is safe but
                    # noisy under contention).
                    conn.execute("SELECT pg_advisory_lock(%s)", (_BOOTSTRAP_LOCK_KEY,))
                    try:
                        conn.execute(_SCHEMA)
                    finally:
                        conn.execute(
                            "SELECT pg_advisory_unlock(%s)", (_BOOTSTRAP_LOCK_KEY,)
                        )
                _pool = pool
    return _pool


def reset_pool_for_tests() -> None:
    """Close + forget the pool. Tests call this between containers."""
    global _pool, _last_purge
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _last_purge = 0.0


def _encode_value(v: Any) -> Any:
    if isinstance(v, bytes):
        return {"__b64__": base64.b64encode(v).decode("ascii")}
    if isinstance(v, dict):
        return {k: _encode_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_encode_value(x) for x in v]
    # DDB's Binary wrapper exposes .value; normalize it here so the
    # migration script can feed boto3 items straight in.
    if hasattr(v, "value") and isinstance(getattr(v, "value"), bytes):
        return {"__b64__": base64.b64encode(v.value).decode("ascii")}
    return v


def _decode_value(v: Any) -> Any:
    if isinstance(v, dict):
        if set(v.keys()) == {"__b64__"}:
            return base64.b64decode(v["__b64__"])
        return {k: _decode_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_decode_value(x) for x in v]
    return v


def encode_attrs(attrs: dict[str, Any]) -> Jsonb:
    """Encode an attr dict for the jsonb column (bytes -> b64 sentinel)."""
    return Jsonb({k: _encode_value(v) for k, v in attrs.items()})


def decode_item(pk: str, sk: str, data: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the DDB-shaped Item dict from a row."""
    item = {k: _decode_value(v) for k, v in data.items()}
    item["PK"] = pk
    item["SK"] = sk
    return item


def split_special(attrs: dict[str, Any]) -> tuple[dict[str, Any], str | None, str | None, int | None]:
    """Lift GSI1PK/GSI1SK/ttl out of an attr dict into their columns.

    They STAY in `data` too (DDB keeps them as plain attributes); the
    columns exist for indexing/filtering only.
    """
    gsi1pk = attrs.get("GSI1PK")
    gsi1sk = attrs.get("GSI1SK")
    ttl = attrs.get("ttl")
    return attrs, gsi1pk, gsi1sk, int(ttl) if ttl is not None else None


def maybe_purge_expired() -> None:
    """Opportunistic TTL purge, at most once per process-hour.

    Correctness never depends on this (reads filter TTL_LIVE); it only
    keeps the table from accumulating dead claim/comment rows forever.
    Failures are logged and swallowed - a purge must never take down a
    request path.
    """
    global _last_purge
    now = time.monotonic()
    if _last_purge and now - _last_purge < _PURGE_INTERVAL_SECONDS:
        return
    _last_purge = now
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM grug_kv WHERE ttl IS NOT NULL "
                "AND ttl <= EXTRACT(EPOCH FROM now())"
            )
    except psycopg.Error:
        log.warning("pg_ttl_purge_failed", exc_info=True)
