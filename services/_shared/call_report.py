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

The same thread also sends the calls running now (`begin`/`end`) to the
gateway's `POST /ui/inflight`, so the gateway page shows what Elder is calling
while it runs: the whole set every few seconds while any runs, then one empty
set to clear it. The gateway expires a set that stops arriving, so a pod that
dies leaves no rows behind.
"""
from __future__ import annotations

import itertools
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Any

import httpx

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.call_report")

FLUSH_INTERVAL_S = 5.0
MAX_QUEUE = 1000
BATCH_ROWS = 200
TIMEOUT_S = 5.0
CALLER = "grug-elder"
INFLIGHT_MAX_CALLS = 200
_REPORTER_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

_queue: deque[dict[str, Any]] = deque(maxlen=MAX_QUEUE)
_lock = threading.Lock()
_thread: threading.Thread | None = None
_client: httpx.Client | None = None
# token -> (backend, model, job, monotonic start)
_inflight: dict[int, tuple[str, str, str | None, float]] = {}
_inflight_lock = threading.Lock()
_inflight_ids = itertools.count(1)
_inflight_shown = False


def enabled() -> bool:
    return (
        os.getenv("GRUG_CALL_REPORT", "").strip().lower() in ("1", "true", "yes")
        and bool(_endpoint())
    )


def _endpoint(path: str = "/ui/calls") -> str:
    base = os.getenv("GRUG_CAVE_GATEWAY_URL", "").strip().rstrip("/")
    return f"{base}{path}" if base else ""


def _reporter() -> str:
    """This pod's name, as the gateway keys its in-flight set."""
    name = _REPORTER_UNSAFE.sub("-", os.getenv("HOSTNAME", "").strip())[:64]
    return name or "grug"


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


def begin(*, backend: str, model: str, job: str | None) -> int | None:
    """Mark one direct call as running; returns the token `end` takes, or
    None when reporting is off. Never raises."""
    if not enabled():
        return None
    with _inflight_lock:
        token = next(_inflight_ids)
        _inflight[token] = (backend, model, job, time.monotonic())
    _ensure_thread()
    return token


def end(token: int | None) -> None:
    """The call `token` names is over. Idempotent."""
    if token is None:
        return
    with _inflight_lock:
        _inflight.pop(token, None)


def _run() -> None:
    while True:
        time.sleep(FLUSH_INTERVAL_S)
        flush()
        flush_inflight()


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


def _inflight_rows() -> list[dict[str, Any]]:
    now = time.monotonic()
    with _inflight_lock:
        running = sorted(_inflight.values(), key=lambda call: call[3])
    rows = []
    for backend, model, job, started in running[:INFLIGHT_MAX_CALLS]:
        row: dict[str, Any] = {
            "target": backend, "model": model, "age_s": round(min(now - started, 3600.0), 2),
        }
        if job:
            row["job"] = job
        rows.append(row)
    return rows


def flush_inflight() -> bool:
    """Send the calls running now; returns True when the gateway took them.
    Sends nothing when none run and the last set sent was already empty."""
    global _client, _inflight_shown
    calls = _inflight_rows()
    if not calls and not _inflight_shown:
        return False
    body = {"reporter": _reporter(), "caller": CALLER, "calls": calls}
    try:
        if _client is None:
            _client = httpx.Client(timeout=TIMEOUT_S)
        _client.post(_endpoint("/ui/inflight"), json=body).raise_for_status()
    except Exception as e:  # noqa: BLE001 - the page must never break Elder
        log.warning("call_report_inflight_failed", extra={"kind": type(e).__name__})
        return False
    _inflight_shown = bool(calls)
    return True
