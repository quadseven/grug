"""Durable queue of deep reviews whose GitHub publish failed (#738).

One grug_kv row per pending publication, under pk `PUBLISH_RETRY` and sk
= the publication's dedup key (see personas/code_reviewer/publish_retry.py
for how the key is derived and why it doubles as the double-post guard).
The row outlives the process that wrote it, so a pod restart between the
failed POST and the next poller tick loses nothing.

Every state change is one statement, so two pollers racing on the same row
cannot both act on it:
- enqueue is INSERT ... ON CONFLICT DO NOTHING: a second enqueue of the
  same publication is a no-op, never a second row.
- claim is a compare-and-set on the lease: it only succeeds when the row
  is pending, due, and not leased by a live owner.
- complete and defer both require the caller to still own the lease.

`data` carries: state (pending | abandoned), attempts, next_attempt_at,
lease_owner, lease_expires_at, last_error, publication (the serialized
PendingPublication) and ttl.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from adapters import pg_base
from adapters.pg_base import TTL_LIVE, get_pool

PUBLISH_RETRY_PK = "PUBLISH_RETRY"
# Long enough to outlast any GitHub outage worth waiting out, short enough
# that an abandoned row does not sit in the table forever.
_PUBLISH_RETRY_TTL_DAYS = 7


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def enqueue_publish_retry(key: str, publication: dict[str, Any]) -> bool:
    """Store a failed publication for retry. True = a new row was written;
    False = this key was already queued (a duplicate enqueue is a no-op)."""
    if not key:
        raise ValueError("publish retry key must be non-empty")
    pg_base.maybe_purge_expired()
    now = _now()
    ttl = now + _PUBLISH_RETRY_TTL_DAYS * 86400
    data = {
        "state": "pending",
        "attempts": 0,
        "next_attempt_at": now,
        "lease_owner": "",
        "lease_expires_at": 0,
        "last_error": "",
        "publication": publication,
        "ttl": ttl,
    }
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, %(sk)s, %(data)s, %(ttl)s)
            ON CONFLICT (pk, sk) DO NOTHING
            RETURNING sk
            """,
            {"pk": PUBLISH_RETRY_PK, "sk": key, "data": Jsonb(data), "ttl": ttl},
        ).fetchone()
    return row is not None


def list_due_publish_retries(limit: int) -> list[str]:
    """Keys of pending rows whose next attempt is due and whose lease (if
    any) has lapsed, oldest due first. A listed key may still lose the
    claim to a concurrent poller; `claim_publish_retry` decides."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT sk FROM grug_kv
            WHERE pk = %(pk)s AND {TTL_LIVE}
              AND data->>'state' = 'pending'
              AND (data->>'next_attempt_at')::bigint <= %(now)s
              AND (data->>'lease_expires_at')::bigint <= %(now)s
            ORDER BY (data->>'next_attempt_at')::bigint, sk
            LIMIT %(limit)s
            """,
            {"pk": PUBLISH_RETRY_PK, "now": _now(), "limit": max(0, int(limit))},
        ).fetchall()
    return [row[0] for row in rows]


def claim_publish_retry(
    key: str, *, owner_token: str, lease_seconds: int,
) -> dict[str, Any] | None:
    """Take the lease on one due row. Returns its data (attempts,
    publication, ...) when this caller won, None when the row is gone, not
    due, abandoned, or leased by someone else."""
    if not owner_token:
        raise ValueError("owner_token must be non-empty")
    now = _now()
    with get_pool().connection() as conn:
        row = conn.execute(
            f"""
            UPDATE grug_kv
            SET data = data || jsonb_build_object(
                'lease_owner', %(owner)s::text,
                'lease_expires_at', %(expires)s::bigint)
            WHERE pk = %(pk)s AND sk = %(sk)s AND {TTL_LIVE}
              AND data->>'state' = 'pending'
              AND (data->>'next_attempt_at')::bigint <= %(now)s
              AND (data->>'lease_expires_at')::bigint <= %(now)s
            RETURNING data
            """,
            {
                "pk": PUBLISH_RETRY_PK, "sk": key, "owner": owner_token,
                "expires": now + max(1, int(lease_seconds)), "now": now,
            },
        ).fetchone()
    return dict(row[0]) if row is not None else None


def complete_publish_retry(key: str, *, owner_token: str) -> bool:
    """Delete a row whose publication is done (posted, found already
    posted, or no longer wanted). False = the lease was lost first."""
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            DELETE FROM grug_kv
            WHERE pk = %(pk)s AND sk = %(sk)s
              AND data->>'lease_owner' = %(owner)s
            RETURNING sk
            """,
            {"pk": PUBLISH_RETRY_PK, "sk": key, "owner": owner_token},
        ).fetchone()
    return row is not None


def defer_publish_retry(
    key: str,
    *,
    owner_token: str,
    attempts: int,
    next_attempt_at: int,
    last_error: str,
    abandon: bool,
) -> bool:
    """Record a failed retry and release the lease. The row stays queued
    for `next_attempt_at`, or is parked as `abandoned` (kept until its TTL
    for the operator to read). False = the lease was lost first."""
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            UPDATE grug_kv
            SET data = data || jsonb_build_object(
                'state', %(state)s::text,
                'attempts', %(attempts)s::int,
                'next_attempt_at', %(next)s::bigint,
                'last_error', %(error)s::text,
                'lease_owner', ''::text,
                'lease_expires_at', 0::bigint)
            WHERE pk = %(pk)s AND sk = %(sk)s
              AND data->>'lease_owner' = %(owner)s
            RETURNING sk
            """,
            {
                "pk": PUBLISH_RETRY_PK, "sk": key, "owner": owner_token,
                "state": "abandoned" if abandon else "pending",
                "attempts": int(attempts), "next": int(next_attempt_at),
                "error": last_error,
            },
        ).fetchone()
    return row is not None
