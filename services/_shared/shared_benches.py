"""Elder's benches and breakers, shared across pods and restarts (#1286).

`llm_client` benches a model after repeated 4xx answers or timeouts, and
opens a breaker on a provider whose key or allowance is spent. That state
lived in each process, so the other three pods, and any pod after a
restart, gave a known-bad model a fresh start and paid for it with cohort
time. This module keeps a copy of every open bench and breaker in the
shared `grug_kv` table, one row per entry, so every pod skips it.

What is shared is only "blocked until": the failure counts and the doubling
of a repeat bench stay per process. A success that clears a bench locally
also clears the shared row.

Opt-in: nothing is read or written unless `GRUG_SHARED_BENCHES` is true and
`GRUG_DATABASE_URL` is set. A review never waits on the store: readers look
at an in-memory snapshot, writers append to a queue, and one daemon thread
applies the queue and re-reads the table every few seconds. A store that is
down leaves the snapshot as it was and costs nothing but a log line. After
a restart the first refresh runs as soon as anything asks, so at most the
reviews of the first few seconds miss the shared state.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.shared_benches")

PK = "ELDER_BENCH"
REFRESH_INTERVAL_S = 15.0
MAX_QUEUE = 1000
# A row outlives its block by this much, so the hourly purge never removes
# one a reader still needs; readers compare `until` themselves.
TTL_SLACK_S = 60
# The kinds llm_client shares: the 4xx bench and the slow bench per
# (backend, model), the provider breaker per (backend, pool), and the
# OpenCode Go usage-limit breaker.
KINDS = frozenset({"bench", "slow", "provider", "go"})

_snapshot: dict[str, float] = {}
_snapshot_lock = threading.Lock()
_queue: deque[tuple[str, str, float]] = deque(maxlen=MAX_QUEUE)
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def enabled() -> bool:
    return (
        os.getenv("GRUG_SHARED_BENCHES", "").strip().lower() in ("1", "true", "yes")
        and bool(os.getenv("GRUG_DATABASE_URL", "").strip())
    )


def _sk(kind: str, first: str, second: str) -> str:
    return f"{kind}#{first}#{second}"


def blocked_until(kind: str, first: str, second: str = "") -> float:
    """When the shared entry ends, from the snapshot (0.0 for none). No I/O."""
    if not enabled():
        return 0.0
    _ensure_thread()
    with _snapshot_lock:
        return _snapshot.get(_sk(kind, first, second), 0.0)


def publish(kind: str, first: str, second: str, until: float) -> None:
    """Share one bench or breaker that is open until `until`. Never raises."""
    if not enabled() or kind not in KINDS:
        return
    sk = _sk(kind, first, second)
    with _snapshot_lock:
        _snapshot[sk] = max(_snapshot.get(sk, 0.0), until)
    _queue.append(("set", sk, until))
    _ensure_thread()


def clear(kind: str, first: str, second: str = "") -> None:
    """Drop a shared entry this pod has seen clear. Never raises, and writes
    nothing when the snapshot holds no such entry, so a success costs no
    store round trip."""
    if not enabled():
        return
    sk = _sk(kind, first, second)
    with _snapshot_lock:
        if _snapshot.pop(sk, None) is None:
            return
    _queue.append(("clear", sk, 0.0))
    _ensure_thread()


def _ensure_thread() -> None:
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_run, name="shared-benches", daemon=True)
        _thread.start()


def _run() -> None:
    stop = threading.Event()
    while True:
        sync()
        if stop.wait(REFRESH_INTERVAL_S):
            return


def sync() -> None:
    """Apply the queued writes, then re-read the table. Never raises."""
    try:
        flush()
        refresh()
    except Exception as e:  # noqa: BLE001 - the store must never break Elder
        log.warning("shared_benches_sync_failed", extra={"kind": type(e).__name__})


def flush() -> int:
    """Write every queued change; returns how many were applied. A change the
    store refuses is dropped: the local bench still holds, and the next trip
    shares it again."""
    applied = 0
    while _queue:
        op, sk, until = _queue.popleft()
        if op == "set":
            _write(sk, until)
        else:
            _delete(sk)
        applied += 1
    return applied


def refresh() -> int:
    """Replace the snapshot with the table's live entries; returns how many."""
    rows = _read_live()
    now = time.time()
    with _snapshot_lock:
        _snapshot.clear()
        _snapshot.update({sk: until for sk, until in rows if until > now})
        return len(_snapshot)


def _write(sk: str, until: float) -> None:
    from adapters.pg_base import get_pool  # late: psycopg is not in every image
    from psycopg.types.json import Jsonb

    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, %(sk)s, %(data)s, %(ttl)s)
            ON CONFLICT (pk, sk) DO UPDATE SET
                data = CASE
                    WHEN (grug_kv.data->>'until')::float8 >= %(until)s THEN grug_kv.data
                    ELSE EXCLUDED.data END,
                ttl = GREATEST(grug_kv.ttl, EXCLUDED.ttl)
            """,
            {"pk": PK, "sk": sk, "data": Jsonb({"until": until}), "until": until,
             "ttl": int(until) + TTL_SLACK_S},
        )


def _delete(sk: str) -> None:
    from adapters.pg_base import get_pool

    with get_pool().connection() as conn:
        conn.execute("DELETE FROM grug_kv WHERE pk = %s AND sk = %s", (PK, sk))


def _read_live() -> list[tuple[str, float]]:
    from adapters.pg_base import TTL_LIVE, get_pool

    with get_pool().connection() as conn:
        # TTL_LIVE is pg_base's constant fragment; the key is a bound parameter.
        rows = conn.execute(
            f"SELECT sk, (data->>'until')::float8 FROM grug_kv WHERE pk = %s AND {TTL_LIVE}",
            (PK,),
        ).fetchall()
    return [(sk, float(until)) for sk, until in rows if until is not None]
