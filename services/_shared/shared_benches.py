"""Elder's benches and breakers, shared across pods and restarts (#1286).

`llm_client` benches a model after repeated 4xx answers or timeouts, and
opens a breaker on a provider whose key or allowance is spent. That state
lived in each process, so every other pod, and any pod after a restart,
gave a known-bad model a fresh start and paid for it with cohort time. This
module keeps a copy of every open bench and breaker in the shared `grug_kv`
table, one row per entry, so every pod skips it.

What is shared is only "blocked until": the failure counts and the doubling
of a repeat bench stay per process. A success that clears a bench locally
also clears the shared row.

Opt-in: nothing is read or written unless `GRUG_SHARED_BENCHES` is true and
`GRUG_DATABASE_URL` is set. Readers look at an in-memory snapshot, writers
append to a queue, and one daemon thread applies the queue and re-reads the
table every few seconds. The thread starts at import, and a process waits
at most FIRST_LOAD_WAIT_S in total for its first read of the table, so a
restarted pod or a fresh poller run starts with the shared state; after
that a review never waits on the store. A store that is down leaves the
snapshot as it was and the queue in place, retried on the next pass, and
costs nothing but a log line. At exit the queue gets one bounded last
flush, so a short-lived run does not lose a bench it opened.

Known limits: a pod that opened a bench keeps its own copy until it ends,
even after another pod's success clears the shared row; a repeat bench's
doubling restarts at the base length after a restart; and past MAX_QUEUE
pending changes (a long outage) the oldest are dropped.
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from collections import deque

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.shared_benches")

PK = "ELDER_BENCH"
REFRESH_INTERVAL_S = 15.0
MAX_QUEUE = 1000
# The longest any process waits, in total, for its first read of the table.
FIRST_LOAD_WAIT_S = 1.0
# The longest exit waits for the last flush.
EXIT_FLUSH_WAIT_S = 3.0
# A row outlives its block by this much, so the hourly purge never removes
# one a reader still needs; readers compare `until` themselves.
TTL_SLACK_S = 60
# The kinds llm_client shares: the 4xx bench and the slow bench per
# (backend, model), the provider breaker per (backend, pool), and the
# OpenCode Go usage-limit breaker.
KINDS = frozenset({"bench", "slow", "provider", "go"})

# The snapshot and the queue change together under one lock, so a re-read
# of the table can never drop an entry whose write is still queued.
_lock = threading.Lock()
_snapshot: dict[str, float] = {}
# Pending changes: (sk, until) to share an entry, (sk, None) to clear one.
_queue: deque[tuple[str, float | None]] = deque(maxlen=MAX_QUEUE)
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_first_sync = threading.Event()
_first_load_deadline = 0.0


def enabled() -> bool:
    return (
        os.getenv("GRUG_SHARED_BENCHES", "").strip().lower() in ("1", "true", "yes")
        and bool(os.getenv("GRUG_DATABASE_URL", "").strip())
    )


def _sk(kind: str, first: str, second: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown shared bench kind {kind!r}")
    return f"{kind}#{first}#{second}"


def blocked_until(kind: str, first: str, second: str = "") -> float:
    """When the shared entry ends, from the snapshot (0.0 for none). Touches
    no store; only a process's first reads wait, FIRST_LOAD_WAIT_S at most
    in total, for the table's first read."""
    if not enabled():
        return 0.0
    sk = _sk(kind, first, second)
    _ensure_thread()
    if not _first_sync.is_set():
        _first_sync.wait(max(0.0, _first_load_deadline - time.monotonic()))
    with _lock:
        return _snapshot.get(sk, 0.0)


def publish(kind: str, first: str, second: str, until: float) -> None:
    """Share one bench or breaker that is open until `until`. Touches no store."""
    if not enabled():
        return
    sk = _sk(kind, first, second)
    with _lock:
        _snapshot[sk] = max(_snapshot.get(sk, 0.0), until)
        _queue.append((sk, until))
    _ensure_thread()


def clear(kind: str, first: str, second: str = "") -> None:
    """Drop a shared entry this pod has seen clear. Touches no store, and
    queues nothing when the snapshot holds no such entry, so a success costs
    no store round trip."""
    if not enabled():
        return
    sk = _sk(kind, first, second)
    with _lock:
        if _snapshot.pop(sk, None) is None:
            return
        _queue.append((sk, None))
    _ensure_thread()


def _ensure_thread() -> None:
    global _thread, _first_load_deadline
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        if _first_load_deadline == 0.0:
            _first_load_deadline = time.monotonic() + FIRST_LOAD_WAIT_S
        _thread = threading.Thread(target=_run, name="shared-benches", daemon=True)
        _thread.start()


def _run() -> None:
    while True:
        sync()
        time.sleep(REFRESH_INTERVAL_S)


def sync() -> None:
    """Apply the queued changes, then re-read the table. Logs a store error
    instead of raising it."""
    try:
        flush()
        refresh()
    except Exception as e:  # noqa: BLE001 - the store must never break Elder
        log.warning("shared_benches_sync_failed", extra={"error": type(e).__name__})
    finally:
        _first_sync.set()


def flush() -> int:
    """Apply the queued changes in order; returns how many were applied. On
    the first one the store refuses, that change goes back to the front of
    the queue with the rest behind it, and the error propagates: a clear
    made while the store is down is applied once it is back, so the row does
    not return."""
    applied = 0
    while True:
        with _lock:
            if not _queue:
                return applied
            change = _queue.popleft()
        sk, until = change
        try:
            if until is None:
                _delete(sk)
            else:
                _write(sk, until)
        except Exception:
            with _lock:
                _queue.appendleft(change)
            raise
        applied += 1


def refresh() -> int:
    """Replace the snapshot with the table's live entries, with the changes
    still queued applied over them; returns how many entries it holds."""
    rows = _read_live()
    now = time.time()
    with _lock:
        fresh = {sk: until for sk, until in rows if until > now}
        for sk, until in _queue:
            if until is None:
                fresh.pop(sk, None)
            else:
                fresh[sk] = max(fresh.get(sk, 0.0), until)
        _snapshot.clear()
        _snapshot.update(fresh)
        return len(_snapshot)


def _flush_at_exit() -> None:
    """One last bounded flush, so a poller run that ends seconds after it
    benched a model still shares the bench."""
    if not enabled() or not _queue:
        return
    last = threading.Thread(target=sync, name="shared-benches-exit", daemon=True)
    last.start()
    last.join(EXIT_FLUSH_WAIT_S)


atexit.register(_flush_at_exit)


def _write(sk: str, until: float) -> None:
    """Upsert one entry, keeping the later end when two pods share the same one."""
    from adapters import pg_base  # late: psycopg is not in every image
    from psycopg.types.json import Jsonb

    pg_base.maybe_purge_expired()
    with pg_base.get_pool().connection() as conn:
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
        rows = conn.execute(
            f"SELECT sk, (data->>'until')::float8 FROM grug_kv WHERE pk = %s AND {TTL_LIVE}",  # noqa: S608 - TTL_LIVE is a fixed pg_base clause, not input
            (PK,),
        ).fetchall()
    return [(sk, float(until)) for sk, until in rows if until is not None]


if enabled():
    _ensure_thread()
