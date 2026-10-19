#!/usr/bin/env python3
"""
Postgres-backed configuration store.

When a Postgres DSN is configured (GRUGTHINK_DATABASE_URL / GRUG_DATABASE_URL,
the same switch that selects the pgvector memory backend), the bot
configuration document lives in the ``grugthink_config`` table instead of a
YAML file on a volume. This is what lets the k8s deployment drop the
node-pinned ``grugthink-data`` local-path PVC: config stays writable (the
dashboard keeps working) and durable across restarts and reschedules, with no
PersistentVolume involved.

The table holds a single row (id = 1) with the whole config document as
JSONB. On first boot with an empty table the store seeds itself from the YAML
seed file when one exists (the migration path off the PVC-era ``/data``
config), otherwise it stores a fresh default config.

Without a DSN this module is inert and the file backend in loader.py is used
unchanged (local dev and tests keep the no-database fallback).
"""

import json
import os
import threading
from typing import Any, Dict

from ..grug_structured_logger import get_logger

log = get_logger(__name__)

_TABLE = "grugthink_config"

_pool = None
_pool_lock = threading.Lock()
_bootstrapped = False


def _database_url() -> str:
    """DSN for Grug's config. Same switch as the pgvector memory backend."""
    return os.environ.get("GRUGTHINK_DATABASE_URL") or os.environ.get("GRUG_DATABASE_URL", "")


def pg_config_enabled() -> bool:
    """True when a Postgres DSN is configured (selects this backend)."""
    return bool(_database_url())


def _bootstrap_schema(conn) -> None:
    """Idempotently create the single-row config table."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id         integer PRIMARY KEY CHECK (id = 1),
            data       jsonb NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()


def get_pool():
    """Lazily build the connection pool and bootstrap the schema once."""
    global _pool, _bootstrapped
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool

        import psycopg
        from psycopg_pool import ConnectionPool

        dsn = _database_url()
        if not dsn:
            raise RuntimeError("GRUGTHINK_DATABASE_URL / GRUG_DATABASE_URL is not set")

        if not _bootstrapped:
            with psycopg.connect(dsn, connect_timeout=10) as conn:
                _bootstrap_schema(conn)
            _bootstrapped = True

        _pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=int(os.getenv("GRUGTHINK_PG_CONFIG_POOL_MAX", "2")),
            kwargs={"connect_timeout": 10},
            open=True,
        )
        log.info("Grug Postgres config pool initialized", extra={"table": _TABLE})
        return _pool


def load_config() -> Dict[str, Any]:
    """Load the config document from Postgres. Returns {} when unset."""
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT data FROM {_TABLE} WHERE id = 1")
            row = cur.fetchone()
    if not row or not row[0]:
        return {}
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    return data if isinstance(data, dict) else {}


def save_config(config_data: Dict[str, Any]) -> None:
    """Upsert the whole config document as the single row."""
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {_TABLE} (id, data, updated_at)
                VALUES (1, %s::jsonb, now())
                ON CONFLICT (id) DO UPDATE
                SET data = EXCLUDED.data, updated_at = now()
                """,
                (json.dumps(config_data),),
            )
        conn.commit()
    log.info("Configuration saved to Postgres", extra={"table": _TABLE})


def load_or_seed(seed_path: str, templates: Dict) -> Dict[str, Any]:
    """Load config from Postgres, seeding the table on first boot.

    Seed order: the YAML seed file when it exists (the migration path for a
    config written during the PVC era), otherwise a fresh default config.
    Never returns an empty document.
    """
    data = load_config()
    if data:
        return data

    # Local import: loader imports this module at top level, so importing it
    # here (at call time) avoids a circular import.
    from . import loader as file_loader

    seed_data = file_loader._load_file_config(seed_path)
    if seed_data:
        save_config(seed_data)
        log.info(
            "Seeded Postgres config from file (PVC-era migration)",
            extra={"seed_path": seed_path},
        )
        return seed_data

    default_config = file_loader.build_default_config(templates)
    save_config(default_config)
    log.info("Seeded Postgres config with default configuration")
    return default_config


def reset_pool_for_tests() -> None:
    """Drop the module pool (tests only)."""
    global _pool, _bootstrapped
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _bootstrapped = False
