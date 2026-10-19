"""Report Elder's direct model calls to the inference gateway's ledger (#1286).

Elder reaches some backends through the gateway (the Cave arms, the judge,
the free-pool tier), and the gateway records those calls itself. The rest
(OpenRouter, Poolside, OpenCode Go) go straight to the provider, so nothing
recorded which of them answered well. This module posts one small row per
such call to the gateway's `POST /ui/calls`, so its `/ui/stats` covers every
model call in one place.

Opt-in: nothing is recorded or sent unless `GRUG_CALL_REPORT` is true and
`GRUG_CAVE_GATEWAY_URL` is set, so a deployment that has not asked for it is
unchanged. A review never waits on it: `record` appends to a bounded deque
(the oldest rows drop first) and one daemon thread posts batches every few
seconds with a short timeout, dropping a batch the gateway refuses. It uses
its own `httpx.Client`, never the module-level `httpx.post` the review path
uses.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from typing import Any

import httpx

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.call_report")

FLUSH_INTERVAL_S = 5.0
MAX_QUEUE = 1000
BATCH_ROWS = 200
TIMEOUT_S = 5.0
CALLER = "grug-elder"

_queue: deque[dict[str, Any]] = deque(maxlen=MAX_QUEUE)
_lock = threading.Lock()
_thread: threading.Thread | None = None
_client: httpx.Client | None = None


def enabled() -> bool:
    return (
        os.getenv("GRUG_CALL_REPORT", "").strip().lower() in ("1", "true", "yes")
        and bool(_endpoint())
    )


def _endpoint() -> str:
    base = os.getenv("GRUG_CAVE_GATEWAY_URL", "").strip().rstrip("/")
    return f"{base}/ui/calls" if base else ""


def outcome_for(error_kind: str | None, status: int | None, degenerate: bool) -> tuple[str, str | None]:
    """(outcome, reason) in the gateway ledger's terms for one attempt.

    A completed call is `ok`, except a degenerate empty review (no findings,
    a few output tokens over a large input), which Elder already treats as a
    miss: it is `error` with reason `degenerate_empty`, so a model that skims
    shows a low success rate. A 429 is `limit` (allowance, not failure)."""
    if error_kind is None:
        return ("error", "degenerate_empty") if degenerate else ("ok", None)
    if error_kind == "transport":
        return "error", "transport"
    if error_kind == "parse_failed":
        return "error", "parse_failed"
    if status == 429:
        return "limit", "http_429"
    if status in (401, 402, 403):
        return "auth", f"http_{status}"
    if status == 404:
        return "missing", "http_404"
    if status is not None and 400 <= status < 500:
        return "client_error", f"http_{status}"
    return "error", f"http_{status}" if status else "http_error"


def record(
    *,
    backend: str,
    model: str,
    served_model: str | None,
    job: str | None,
    status: int | None,
    outcome: str,
    reason: str | None,
    latency_s: float,
) -> None:
    """Queue one row and return. Never raises."""
    if not enabled():
        return
    row: dict[str, Any] = {
        "target": backend,
        "caller": CALLER,
        "model": model,
        "outcome": outcome,
        "latency_ms": max(0, min(round(latency_s * 1000), 3_600_000)),
    }
    if served_model:
        row["served_model"] = served_model
    if job:
        row["job"] = job
    if status is not None:
        row["status"] = status
    if reason:
        row["reason"] = reason
    _queue.append(row)
    _ensure_thread()


def _ensure_thread() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_run, name="call-report", daemon=True)
        _thread.start()


def _run() -> None:
    stop = threading.Event()
    while not stop.wait(FLUSH_INTERVAL_S):
        flush()


def flush() -> int:
    """Post every queued row in batches. Returns how many the gateway
    accepted. A refused or failed batch is dropped, not retried: these are
    statistics, and a retry loop must never build up behind an outage."""
    global _client
    accepted = 0
    while _queue:
        batch = []
        while _queue and len(batch) < BATCH_ROWS:
            batch.append(_queue.popleft())
        try:
            if _client is None:
                _client = httpx.Client(timeout=TIMEOUT_S)
            resp = _client.post(_endpoint(), json={"calls": batch})
            resp.raise_for_status()
            accepted += int(resp.json().get("accepted", 0))
        except Exception as e:  # noqa: BLE001 - statistics must never break Elder
            log.warning("call_report_failed", extra={"kind": type(e).__name__, "rows": len(batch)})
    return accepted
