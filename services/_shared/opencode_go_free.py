"""Which OpenCode Go models cost nothing right now, and how hard to ask them.

OpenCode Go publishes its model list without a key, and each entry carries a
price. The set of free models changes often, so Elder reads the list at run
time instead of trusting a name. `free_chat_models` is the only filter: a
model is free only when its input and output prices are both a plain number
equal to zero, it has no cache charge, no price multiplier and no other
billed axis, and it speaks the chat-completions wire. A name such as
`*-free` proves nothing.

`FreeModelCatalog` keeps the answer for a while, serves the last good list
for a bounded time when a refresh fails, backs off with jitter after a
failure, and waits out Retry-After after a 429. It never raises.

`FreeCallGate` paces calls to those models: a bounded number in flight, a
ceiling per hour, and a per-model cool-down after a 429 that honours
Retry-After and otherwise backs off with doubling and jitter. Both classes
are per process; the ceiling is therefore per pod.

`qualified_models` reads the checked-in eval report and names the free models
whose measured false-empty rate is low enough for their empty answer to count
as a confirming second opinion (grug#1601). It works from the counts, never
from a stored list, and a missing, stale or malformed report qualifies nobody.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
from collections import deque
from datetime import date
from pathlib import Path
from typing import Any, Callable

import httpx

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.opencode_go_free")

MODELS_URL = "https://opencode.ai/zen/go/v1/models"
DEFAULT_TTL_SECONDS = 2700.0  # 45 minutes
DEFAULT_MAX_STALE_SECONDS = 7200.0
FETCH_TIMEOUT_SECONDS = 5.0
FAILURE_BACKOFF_BASE_SECONDS = 60.0
FAILURE_BACKOFF_MAX_SECONDS = 900.0
MAX_RETRY_AFTER_SECONDS = 3600.0
# Cool-down after a 429 with no usable Retry-After.
COOLDOWN_BASE_SECONDS = 30.0
MAX_COOLDOWN_SECONDS = 3600.0
JITTER_FRACTION = 0.25
HOUR_SECONDS = 3600.0


def _is_zero(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value == 0
    )


def _price_is_zero(node: Any) -> bool:
    """A `{"price": 0, "cache": {...}}` node: price zero, cache charges zero."""
    if not isinstance(node, dict) or not _is_zero(node.get("price")):
        return False
    cache = node.get("cache")
    if cache is None:
        return True
    return isinstance(cache, dict) and all(_is_zero(v) for v in cache.values())


def _model_is_free(model: Any) -> bool:
    if not isinstance(model, dict):
        return False
    if not isinstance(model.get("id"), str) or not model["id"]:
        return False
    if model.get("type") != "chat":
        return False
    api = model.get("api")
    if not isinstance(api, dict) or "chat-completions" not in api:
        return False
    cost = model.get("cost")
    # Any billed axis besides text (audio, a multiplier) refuses the model.
    if not isinstance(cost, dict) or set(cost) != {"text"}:
        return False
    text = cost["text"]
    if not isinstance(text, dict):
        return False
    return _price_is_zero(text.get("input")) and _price_is_zero(text.get("output"))


def _listed(payload: Any) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


def free_chat_models(payload: Any) -> list[str]:
    """Ids of the chat-completions models priced exactly zero, in list order."""
    seen: list[str] = []
    for model in _listed(payload):
        if _model_is_free(model) and model["id"] not in seen:
            seen.append(model["id"])
    return seen


def _priced_ids(payload: Any, free: list[str]) -> frozenset[str]:
    return frozenset(
        m["id"] for m in _listed(payload)
        if isinstance(m.get("id"), str) and m["id"] not in free
    )


def retry_after_seconds(resp: httpx.Response) -> float | None:
    """Retry-After in seconds, capped; None when absent or not a number."""
    raw = (resp.headers.get("retry-after") or "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _default_fetch() -> httpx.Response:
    url = os.getenv("GRUG_OPENCODE_GO_MODELS_URL", MODELS_URL)
    return httpx.get(url, timeout=FETCH_TIMEOUT_SECONDS)


class FreeModelCatalog:
    """The free chat models, cached. Thread-safe; one fetch at a time."""

    def __init__(
        self,
        fetch: Callable[[], httpx.Response] = _default_fetch,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS,
        clock: Callable[[], float] = time.time,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._max_stale = max_stale_seconds
        self._clock = clock
        self._rand = rand
        self._lock = threading.Lock()
        self._fetching = threading.Lock()
        self._free: list[str] = []
        self._priced: frozenset[str] = frozenset()
        self._fetched_at: float | None = None
        self._failures = 0
        self.retry_not_before = 0.0

    def _usable(self, now: float) -> bool:
        return self._fetched_at is not None and now - self._fetched_at <= self._ttl + self._max_stale

    def _fresh(self, now: float) -> bool:
        return self._fetched_at is not None and now - self._fetched_at <= self._ttl

    def models(self) -> list[str]:
        """Free model ids now: fresh cache, else a refresh, else the stale
        list while it is young enough, else nothing."""
        now = self._clock()
        with self._lock:
            if self._fresh(now):
                return list(self._free)
            if now < self.retry_not_before:
                return list(self._free) if self._usable(now) else []
        if not self._fetching.acquire(blocking=False):
            with self._lock:
                return list(self._free) if self._usable(now) else []
        try:
            self._refresh()
        finally:
            self._fetching.release()
        with self._lock:
            return list(self._free) if self._usable(self._clock()) else []

    def _refresh(self) -> None:
        try:
            resp = self._fetch()
        except Exception as e:  # noqa: BLE001 - discovery must never break a review
            self._failed(f"{type(e).__name__}", None)
            return
        if resp.status_code == 429:
            self._failed("429", retry_after_seconds(resp))
            return
        try:
            payload = resp.json() if resp.status_code == 200 else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            self._failed(f"status {resp.status_code} or unreadable list", None)
            return
        free = free_chat_models(payload)
        with self._lock:
            self._free = free
            self._priced = _priced_ids(payload, free)
            self._fetched_at = self._clock()
            self._failures = 0
            self.retry_not_before = 0.0
        log.info("opencode_go_free_models", extra={"count": len(free), "models": free})

    def _failed(self, why: str, retry_after: float | None) -> None:
        with self._lock:
            self._failures += 1
            if retry_after is not None:
                wait = retry_after
            else:
                wait = min(
                    FAILURE_BACKOFF_BASE_SECONDS * 2 ** (self._failures - 1),
                    FAILURE_BACKOFF_MAX_SECONDS,
                )
            wait *= 1 + JITTER_FRACTION * self._rand()
            self.retry_not_before = self._clock() + wait
        log.warning("opencode_go_free_models_failed", extra={"why": why, "retry_in": round(wait)})

    def verdict(self, model: str) -> bool | None:
        """From the cache only, never the network: True when listed free,
        False when listed with a price, None when unknown or too stale."""
        with self._lock:
            if not self._usable(self._clock()):
                return None
            if model in self._free:
                return True
            return False if model in self._priced else None


class FreeCallGate:
    """Pacing for calls to free models: in flight, per hour, per-model cool-down."""

    def __init__(
        self,
        *,
        hourly_ceiling: int = 240,
        max_concurrent: int = 2,
        clock: Callable[[], float] = time.time,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._ceiling = max(0, hourly_ceiling)
        self._slots = threading.BoundedSemaphore(max(1, max_concurrent))
        self._clock = clock
        self._rand = rand
        self._lock = threading.Lock()
        self._calls: deque[float] = deque()
        self._until: dict[str, float] = {}
        self._streak: dict[str, int] = {}

    def _trim(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= HOUR_SECONDS:
            self._calls.popleft()

    def ceiling_reached(self) -> bool:
        with self._lock:
            self._trim(self._clock())
            return len(self._calls) >= self._ceiling

    def enter(self, model: str, wait_seconds: float = 5.0) -> bool:
        """Take a slot and count one call, or refuse. A refusal costs nothing."""
        if self.cooling(model) or self.ceiling_reached():
            return False
        if not self._slots.acquire(timeout=max(0.0, wait_seconds)):
            return False
        with self._lock:
            now = self._clock()
            self._trim(now)
            if len(self._calls) >= self._ceiling:
                self._slots.release()
                return False
            self._calls.append(now)
        return True

    def leave(self) -> None:
        self._slots.release()

    def cooldown_until(self, model: str) -> float:
        with self._lock:
            return self._until.get(model, 0.0)

    def cooling(self, model: str) -> bool:
        return self._clock() < self.cooldown_until(model)

    def note_response(self, model: str, resp: httpx.Response) -> None:
        """A 429 starts a cool-down; a good answer ends the streak."""
        if resp.status_code < 400:
            with self._lock:
                self._streak.pop(model, None)
                self._until.pop(model, None)
            return
        if resp.status_code != 429:
            return
        with self._lock:
            streak = self._streak.get(model, 0) + 1
            self._streak[model] = streak
            wait = retry_after_seconds(resp)
            if wait is None:
                wait = min(COOLDOWN_BASE_SECONDS * 2 ** (streak - 1), MAX_COOLDOWN_SECONDS)
            wait = min(wait * (1 + JITTER_FRACTION * self._rand()), MAX_COOLDOWN_SECONDS * (1 + JITTER_FRACTION))
            self._until[model] = max(self._until.get(model, 0.0), self._clock() + wait)
        log.warning("opencode_go_free_cooldown", extra={"model": model, "seconds": round(wait)})


# --- the measured allowlist (grug#1601) ----------------------------------------

EVAL_REPORT_FILE = Path(__file__).with_name("opencode_go_free_eval.json")
# A model qualifies on at least 20 defect cases with a false-empty rate of at
# most 5 percent. The rate is compared as integers: false_empty * 20 <= scored.
MIN_DEFECT_CASES = 20
MAX_FALSE_EMPTY_RATE_PERCENT = 5
# A report older than this qualifies nobody: models change under the same name.
DEFAULT_REPORT_MAX_AGE_DAYS = 60


def _today() -> date:
    return date.today()


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def model_qualifies(stats: Any) -> bool:
    """True when `stats` (`defect_scored`, `false_empty`) clears the bar."""
    if not isinstance(stats, dict):
        return False
    scored, empty = _count(stats.get("defect_scored")), _count(stats.get("false_empty"))
    if scored is None or empty is None or scored < MIN_DEFECT_CASES or empty > scored:
        return False
    return empty * 100 <= scored * MAX_FALSE_EMPTY_RATE_PERCENT


def qualified_models(
    report: Any,
    *,
    today: date | None = None,
    max_age_days: int = DEFAULT_REPORT_MAX_AGE_DAYS,
) -> frozenset[str]:
    """The models an eval report shows to clear the bar, or nothing when the
    report is unreadable or older than `max_age_days`."""
    if not isinstance(report, dict) or not isinstance(report.get("models"), dict):
        return frozenset()
    try:
        measured = date.fromisoformat(str(report.get("date")))
    except ValueError:
        return frozenset()
    age = ((today or _today()) - measured).days
    if age < 0 or age > max_age_days:
        return frozenset()
    return frozenset(m for m, stats in report["models"].items() if model_qualifies(stats))


def load_eval_report(path: Path | str | None = None) -> Any:
    """The parsed report, or None when it is absent or not JSON."""
    try:
        return json.loads(Path(path or EVAL_REPORT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
