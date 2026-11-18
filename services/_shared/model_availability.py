"""Model eligibility, call limits, and shared breaker and bench state."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import httpx

import opencode_go_free
import shared_benches
from llm_backend import Backend, BackendConfig
from openrouter_free_limiter import RateLimitTimeoutError, acquire_free_tier_slot, is_free_tier_model
from redact import redact_secrets as _redact_secrets

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.llm_client")

_subscription_cap_lock = threading.Lock()
_subscription_daily_calls: dict[str, int] = {}


def _subscription_daily_cap() -> int:
    try:
        val = int(os.getenv("GRUG_SUBSCRIPTION_DAILY_CAP", "50"))
        return max(0, val)
    except (ValueError, TypeError):
        return 50


def _subscription_today_key() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _subscription_calls_today() -> int:
    with _subscription_cap_lock:
        return _subscription_daily_calls.get(_subscription_today_key(), 0)


def _is_subscription_cap_reached() -> bool:
    return _subscription_calls_today() >= _subscription_daily_cap()


def _record_subscription_call() -> None:
    with _subscription_cap_lock:
        key = _subscription_today_key()
        _subscription_daily_calls[key] = _subscription_daily_calls.get(key, 0) + 1


# Free-model discovery. OpenCode Go publishes a price for every model, and the
# free set changes often, so Elder reads the list (cached, `opencode_go_free`)
# and tries the models priced exactly zero behind the configured ones. Opt-in:
# nothing is fetched or added unless `GRUG_OPENCODE_GO_FREE_DISCOVERY` is true.
# Knobs: `_TTL_SECONDS` (cache, default 2700), `_MAX_TIERS` (discovered tiers
# per walk, default 2), `_HOURLY_CEILING` (calls per hour per pod to all free
# Go models, default 240), `_CONCURRENCY` (calls in flight, default 2).
# Private code still reaches only `_OPENCODE_GO_PRIVATE_SAFE_MODELS`.
_opencode_go_free_catalog: "opencode_go_free.FreeModelCatalog | None" = None
_opencode_go_free_gate: "opencode_go_free.FreeCallGate | None" = None
_opencode_go_free_lock = threading.Lock()


def _opencode_go_free_models_fetch() -> httpx.Response:
    return opencode_go_free._default_fetch()


def _env_number(name: str, default: float, cast: Callable[[str], Any] = float) -> Any:
    try:
        return cast(os.getenv(name, "").strip() or default)
    except ValueError:
        log.warning("grug_opencode_go_free_setting_invalid", extra={"setting": name})
        return default


def _opencode_go_free_discovery_on() -> bool:
    return os.getenv("GRUG_OPENCODE_GO_FREE_DISCOVERY", "").strip().lower() in ("1", "true", "yes")


def discover_go_models(excluded: set[str]) -> list[str]:
    """Free models with room for another call, with private-safe models first."""
    if not _opencode_go_free_discovery_on():
        return []
    limit = _env_number("GRUG_OPENCODE_GO_FREE_MAX_TIERS", 2, int)
    gate = _opencode_go_free_gate_get()
    if limit <= 0 or gate.ceiling_reached():
        return []
    found = [
        model for model in _opencode_go_free_catalog_get().models()
        if model not in excluded and not gate.cooling(model)
    ]
    found.sort(key=lambda model: model not in _OPENCODE_GO_PRIVATE_SAFE_MODELS)
    return found[:limit]


def _opencode_go_free_catalog_get() -> "opencode_go_free.FreeModelCatalog":
    global _opencode_go_free_catalog
    with _opencode_go_free_lock:
        if _opencode_go_free_catalog is None:
            _opencode_go_free_catalog = opencode_go_free.FreeModelCatalog(
                lambda: _opencode_go_free_models_fetch(),
                ttl_seconds=_env_number(
                    "GRUG_OPENCODE_GO_FREE_TTL_SECONDS", opencode_go_free.DEFAULT_TTL_SECONDS,
                ),
            )
        return _opencode_go_free_catalog


def _opencode_go_free_gate_get() -> "opencode_go_free.FreeCallGate":
    global _opencode_go_free_gate
    with _opencode_go_free_lock:
        if _opencode_go_free_gate is None:
            _opencode_go_free_gate = opencode_go_free.FreeCallGate(
                hourly_ceiling=_env_number("GRUG_OPENCODE_GO_FREE_HOURLY_CEILING", 240, int),
                max_concurrent=_env_number("GRUG_OPENCODE_GO_FREE_CONCURRENCY", 2, int),
            )
        return _opencode_go_free_gate


@contextlib.contextmanager
def _opencode_go_free_slot(config: BackendConfig):
    """Pace a call to a free Go model: refuse before the network at the hourly
    ceiling, a cool-down or full concurrency, and release the slot after."""
    if config.backend != Backend.OPENCODE_GO or not _opencode_go_model_is_free(config.model):
        yield
        return
    gate = _opencode_go_free_gate_get()
    if not gate.enter(config.model):
        raise OpencodeGoCircuitOpenError(
            f"free Go model {config.model} is paced off (hourly ceiling, cool-down or busy)"
        )
    try:
        yield
    finally:
        gate.leave()


# OpenCode Go circuit breaker. A usage-limit refusal (HTTP 429,
# `GoUsageLimitError`) does not clear for hours or days: on 2026-09-26 it
# carried `Retry-After: 105419` and `limitName: monthly`. Calling Go on every
# review until then bought nothing but a refused request each time, so the
# first such refusal opens this breaker and Go is skipped until Retry-After
# has passed. The open breaker is shared with every pod and survives a
# restart (`shared_benches`, grug#1286), so the account pays one refused call
# per window rather than one per pod.
_OPENCODE_GO_DEFAULT_BLOCK_SECONDS = 3600.0
_OPENCODE_GO_MAX_BLOCK_SECONDS = 7 * 86400.0
_opencode_go_breaker_lock = threading.Lock()
_opencode_go_blocked_until = 0.0


class OpencodeGoCircuitOpenError(httpx.RequestError):
    """OpenCode Go is skipped: its usage-limit breaker is open, its daily cap
    is reached, or the cap's store cannot answer. Subclasses
    `httpx.RequestError` so every caller degrades exactly as it does for a
    transport failure."""


# The Go models a private repo's code may reach, each read against OpenCode's
# published terms on 2026-11-01. Check the terms before adding one. A model off
# this list serves known-public repos only, so a training model can never see
# private code. Unknown visibility counts as private.
#   deepseek-v4.1-flash: not used for training, 0 days under a zero-retention
#     agreement valid through 2026-11-30, renewed monthly.
#   beam-free: not used for training, retained 30 days for safety.
_OPENCODE_GO_PRIVATE_SAFE_MODELS = frozenset({"deepseek-v4.1-flash", "beam-free"})


def _opencode_go_model_is_free(model: str) -> bool:
    """OpenCode Go's free models (`*-free`, e.g. `longcat-2.5-preview-free`)
    never count against the paid quota the usage-limit breaker protects, so
    the breaker must not gate them. When the published price list is cached,
    it overrules the name: a listed price makes the model paid, a listed zero
    makes it free (a free model need not carry the suffix). With no list the
    name rule stands."""
    listed = _opencode_go_free_catalog_get().verdict(model)
    if listed is not None:
        return listed
    return model.endswith("-free")


def _opencode_go_available(now: float | None = None) -> bool:
    current = time.time() if now is None else now
    with _opencode_go_breaker_lock:
        local = _opencode_go_blocked_until
    return current >= max(local, shared_benches.blocked_until("go", Backend.OPENCODE_GO.value))


def _retry_after_seconds(resp: httpx.Response) -> float:
    raw = (resp.headers.get("retry-after") or "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        return _OPENCODE_GO_DEFAULT_BLOCK_SECONDS
    if not math.isfinite(seconds) or seconds <= 0:
        return _OPENCODE_GO_DEFAULT_BLOCK_SECONDS
    return min(seconds, _OPENCODE_GO_MAX_BLOCK_SECONDS)


def _is_opencode_go_usage_limit(resp: httpx.Response) -> bool:
    if resp.status_code != 429:
        return False
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    return (
        error.get("type") == "GoUsageLimitError"
        or "usage limit" in str(error.get("message", "")).lower()
    )


def _log_opencode_go_usage(model: str, resp: httpx.Response) -> None:
    """What the call cost, from the `usage.opencode` block on each answer."""
    try:
        allowance = float(resp.json()["usage"]["opencode"]["allowance"])
    except (ValueError, KeyError, TypeError):
        return
    log.info("opencode_go_usage", extra={"model": model, "allowance": allowance})


def _note_opencode_go_response(resp: httpx.Response) -> None:
    """Open the breaker on a usage-limit 429; ordinary overload 429s pass."""
    if not _is_opencode_go_usage_limit(resp):
        return
    seconds = _retry_after_seconds(resp)
    limit_name = ""
    try:
        metadata = resp.json().get("metadata") or {}
        limit_name = str(metadata.get("limitName", ""))
    except (ValueError, AttributeError, json.JSONDecodeError):
        pass
    _hold_opencode_go_until(time.time() + seconds)
    log.warning(
        "opencode_go_circuit_open",
        extra={"block_seconds": seconds, "limit_name": limit_name},
    )


# OpenCode Go daily cap (grug#1575). The breaker above reacts only after the
# plan is spent, and the key is shared with other work. The 5-hour, weekly and
# monthly allowances are percentages of each model's monthly dollar limit, so
# a fixed count of paid calls per UTC day bounds a month's spend.
# `GRUG_OPENCODE_GO_DAILY_CAP` is that count, shared by every pod through
# `pg_rate_limit_store`. Unset means no cap; a garbled value admits nothing.
# At the cap the breaker holds Go until the next UTC day. A store error refuses
# the call without a hold: no counter, no bound, but no lost day either. Free
# models spend no allowance and are never counted.
_OPENCODE_GO_CAP_LIMITER = "opencode_go_paid"


def _opencode_go_daily_cap() -> int | None:
    raw = os.getenv("GRUG_OPENCODE_GO_DAILY_CAP", "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("grug_opencode_go_daily_cap_invalid", extra={"value": raw})
        return 0


def _next_utc_midnight() -> float:
    return (math.floor(time.time() / 86400) + 1) * 86400.0


def _hold_opencode_go_until(until: float) -> None:
    global _opencode_go_blocked_until
    with _opencode_go_breaker_lock:
        _opencode_go_blocked_until = max(_opencode_go_blocked_until, until)
    shared_benches.publish("go", Backend.OPENCODE_GO.value, "", until)


def _reserve_opencode_go_call(config: BackendConfig) -> None:
    """`_call_backend`'s daily cap: count one paid Go call, or raise before
    any network call, so the caller moves to its next tier."""
    if config.backend != Backend.OPENCODE_GO or _opencode_go_model_is_free(config.model):
        return
    cap = _opencode_go_daily_cap()
    if cap is None:
        return
    try:
        from adapters.pg_rate_limit_store import try_reserve_slot  # late: pg-backed

        # The minute window never binds first: it fills only as the day does.
        result = try_reserve_slot(
            name=_OPENCODE_GO_CAP_LIMITER, minute_limit=max(cap, 1), day_limit=cap,
        )
    except Exception as e:  # noqa: BLE001 - fail CLOSED, see the comment above
        log.warning("opencode_go_daily_cap_store_error", extra={"kind": type(e).__name__})
        raise OpencodeGoCircuitOpenError("opencode Go daily cap: store unavailable") from e
    if result.admitted:
        return
    _hold_opencode_go_until(_next_utc_midnight())
    log.warning(
        "opencode_go_daily_cap_reached",
        extra={"cap": cap, "day_count": result.day_count},
    )
    raise OpencodeGoCircuitOpenError("opencode Go daily cap reached")


# Per-model bench. A withdrawn model (an ended free promotion, a retired
# name) starts answering every call with the same client error. Three such
# failures within ten minutes bench the (backend, model) pair for an hour,
# doubling to twelve hours on repeat; a success un-benches it at once.
# Transport errors and 5xx are outages, not withdrawal, and never count.
# Rate limits and usage/quota refusals have their own handling, and a
# request-specific error (oversized context) says nothing about the model.
# Counted per process; an open bench is shared with every pod and survives
# a restart through `shared_benches` (grug#1286), like both breakers.
_BENCH_FAILURE_THRESHOLD = 3
_BENCH_WINDOW_SECONDS = 600.0
_BENCH_BASE_SECONDS = 3600.0
_BENCH_MAX_SECONDS = 12 * 3600.0
_BENCH_CLOUD_BACKENDS = frozenset({Backend.OPENCODE_GO, Backend.OPENROUTER, Backend.POOLSIDE})
_LIMIT_HOLD_BACKENDS = _BENCH_CLOUD_BACKENDS | {Backend.FREE_POOL}
# Statuses that are never withdrawal: timeout, too-large, rate limit.
_BENCH_IGNORED_STATUSES = frozenset({408, 413, 425, 429})
_BENCH_QUOTA_MARKERS = ("usage limit", "rate limit", "quota", "insufficient credit", "key limit")
_BENCH_REQUEST_MARKERS = (
    "context length", "context window", "context_length", "maximum context",
    "too long", "too many tokens", "reduce the length", "prompt is too large",
)


@dataclass
class _BenchEntry:
    failures: list[float] = field(default_factory=list)
    benched_until: float = 0.0
    next_seconds: float = _BENCH_BASE_SECONDS


_bench_lock = threading.Lock()
_bench_state: dict[tuple[str, str], _BenchEntry] = {}


def _model_is_benched(backend: str, model: str, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    with _bench_lock:
        entry = _bench_state.get((backend, model))
        local = entry.benched_until if entry is not None else 0.0
    return current < max(local, shared_benches.blocked_until("bench", backend, model))


def _is_benchable_failure(resp: httpx.Response) -> bool:
    """A 4xx that is not a rate limit, quota refusal, or request-specific."""
    status = resp.status_code
    if not 400 <= status < 500 or status in _BENCH_IGNORED_STATUSES:
        return False
    text = (resp.text or "").lower()
    return not any(m in text for m in _BENCH_QUOTA_MARKERS + _BENCH_REQUEST_MARKERS)


def _record_model_failure(backend: str, model: str, status: int, now: float) -> float | None:
    """Count one failure; return the bench duration when it trips the bench."""
    with _bench_lock:
        entry = _bench_state.setdefault((backend, model), _BenchEntry())
        if now < entry.benched_until:
            return None
        entry.failures = [t for t in entry.failures if now - t < _BENCH_WINDOW_SECONDS]
        entry.failures.append(now)
        if len(entry.failures) < _BENCH_FAILURE_THRESHOLD:
            return None
        seconds = entry.next_seconds
        entry.benched_until = now + seconds
        entry.next_seconds = min(seconds * 2, _BENCH_MAX_SECONDS)
        entry.failures = []
        return seconds


def _announce_model_benched(backend: str, model: str, status: int, seconds: float) -> None:
    log.warning(
        "llm_model_benched",
        extra={"backend": backend, "model": model, "status": status, "bench_seconds": seconds},
    )
    try:
        from observability import emit_count  # type: ignore  # late: webhook-image only

        emit_count(
            "grug.elder.model_benched", 1,
            tags={"backend": backend, "model": model, "status": str(status)},
        )
    except Exception as e:  # noqa: BLE001 - telemetry must never break the review
        log.debug("model_benched_metric_failed", extra={"kind": type(e).__name__})


def _note_model_response(config: BackendConfig, resp: httpx.Response) -> None:
    """Feed one cloud response to the per-model bench."""
    if config.backend not in _BENCH_CLOUD_BACKENDS:
        return
    key = (config.backend.value, config.model)
    if resp.status_code < 400:
        with _bench_lock:
            _bench_state.pop(key, None)
            _slow_bench_state.pop(key, None)
        shared_benches.clear("bench", *key)
        shared_benches.clear("slow", *key)
        return
    if not _is_benchable_failure(resp):
        return
    now = time.time()
    seconds = _record_model_failure(*key, resp.status_code, now)
    if seconds is not None:
        shared_benches.publish("bench", *key, now + seconds)
        _announce_model_benched(*key, resp.status_code, seconds)


# Switched-off backends and the provider breaker (grug#1225).
#
# `GRUG_DISABLED_BACKENDS` turns a backend off by configuration: a comma-
# separated list of backend names (`opencode-go`, `openrouter`, `poolside`),
# each optionally narrowed to one pool with `:free` or `:paid`
# (`openrouter:paid` keeps the `:free` models). A disabled backend is never
# called: the cloud review chain, the SaaS overload fallback, the judge and
# the interactive order all skip it, and `_call_backend` refuses it as the
# last guard. Read on every call, so re-enabling one is removing its entry
# and rolling out. Unset (the default) disables nothing.
#
# The provider breaker covers a provider nobody has switched off yet. A 401
# (key wrong or expired), a 402 (unpaid), or a refusal whose body says a
# limit is spent (OpenRouter's `403 Key limit exceeded (total limit)` and
# `429 ... free-models-per-day`, OpenCode Go's monthly usage limit) does not
# clear within a review, so the first one stops calls to that provider for
# Retry-After, else an hour. A 401 covers the whole key; the others only the
# pool (free or paid) that answered, because OpenRouter's free models keep
# answering on a key whose paid limit is zero. A plain 429 is overload and
# never trips it. Per process, like the bench above. Measured before it
# existed (402 Elder runs, 2026-10-17): 36 OpenCode Go 401s, 11 OpenRouter
# paid "total limit" refusals and 139 OpenRouter free per-day 429s, each one
# spent cohort time before a working backend was reached. An open breaker is
# shared with every pod and survives a restart (`shared_benches`, grug#1286).
_PROVIDER_POOLS = ("free", "paid", "subscription")
_SPENT_LIMIT_MARKERS = (
    "usage limit", "quota", "credit", "key limit", "total limit", "spend limit",
)
# A 429 trips it only when the body names a daily or monthly allowance.
# Poolside answers short-lived throttling with `429 usage limit exceeded` and
# no Retry-After; reading that as spent blocked the one working cloud tier
# for an hour (live, 2026-10-17). It can also last: on 2026-10-31 it ran 75
# minutes, 1,473 `llm_backend_rate_limited` across both consumer pods and
# still firing, and over seven days 7,203 of 13,199 Poolside calls were
# refused. So that body is held by the short escalating limit hold below, not
# by this one-hour trip. OpenCode Go's monthly 429 carries the
# `GoUsageLimitError` type, and its own breaker above honours Retry-After.
_SPENT_RATE_MARKERS = ("per-day", "per day", "daily", "monthly", "gousagelimiterror")
_provider_breaker_lock = threading.Lock()
_provider_blocked_until: dict[tuple[str, str], float] = {}
# A limit that outlasts a throttle reads the same on one response, so the hold
# escalates instead: the first limit refusal holds the provider for two
# minutes, and each refusal that follows a hold doubles it, up to thirty. The
# first success clears it, so Poolside is back the moment its limit resets.
# While held the provider is not called at all, so no call retries the body,
# and a review that ran during the hold ends degraded. On 2026-10-31 Poolside
# answered 1,892 of these in nine hours and the free pool 57, each costing a
# chunk its retry and its wait before the subscription tier could run. The
# open hold is shared with every pod; the escalation step stays per process.
_LIMIT_HOLD_BASE_SECONDS = 120.0
_LIMIT_HOLD_MAX_SECONDS = 1800.0
_LIMIT_REFUSAL_MARKERS = ("usage limit", "capacity is reserved")
_limit_hold_level: dict[tuple[str, str], int] = {}
_warned_disabled_entries: set[str] = set()


class BackendDisabledError(httpx.RequestError):
    """The backend is switched off (`GRUG_DISABLED_BACKENDS`) or its provider
    breaker is open. Subclasses `httpx.RequestError` so every caller degrades
    exactly as it does for a transport failure."""


def _backend_pool(backend: Backend, model: str) -> str:
    """"free" for a provider's no-cost models, "paid" for everything else."""
    if backend == Backend.OPENROUTER and is_free_tier_model(model):
        return "free"
    if backend == Backend.OPENCODE_GO and _opencode_go_model_is_free(model):
        return "free"
    if backend == Backend.FREE_POOL:
        return "free"
    if backend == Backend.SUBSCRIPTION:
        return "subscription"
    return "paid"


def _disabled_backend_entries() -> frozenset[str]:
    entries = frozenset(
        e.strip().lower()
        for e in os.getenv("GRUG_DISABLED_BACKENDS", "").split(",")
        if e.strip()
    )
    known = {b.value for b in Backend}
    for entry in entries - _warned_disabled_entries:
        name, _, pool = entry.partition(":")
        if name not in known or pool not in ("", *_PROVIDER_POOLS):
            # A typo must not read as "switched off" while the backend runs.
            log.warning("grug_disabled_backends_unknown_entry", extra={"entry": entry})
        _warned_disabled_entries.add(entry)
    return entries


def _backend_enabled(backend: Backend, model: str) -> bool:
    """False when `GRUG_DISABLED_BACKENDS` names this backend or its pool."""
    entries = _disabled_backend_entries()
    if not entries:
        return True
    name = backend.value
    return name not in entries and f"{name}:{_backend_pool(backend, model)}" not in entries


def _provider_available(backend: Backend, model: str, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    key = (backend.value, _backend_pool(backend, model))
    with _provider_breaker_lock:
        local = _provider_blocked_until.get(key, 0.0)
    return current >= max(local, shared_benches.blocked_until("provider", *key))


def backend_usable(backend: Backend, model: str) -> bool:
    """Switched on and its provider breaker closed."""
    return _backend_enabled(backend, model) and _provider_available(backend, model)


def _is_spent_quota_refusal(resp: httpx.Response) -> bool:
    """A refusal that says the money or the allowance is gone, not overload."""
    status = resp.status_code
    if status == 402:
        return True
    if status not in (403, 429):
        return False
    text = (resp.text or "").lower()
    markers = _SPENT_LIMIT_MARKERS if status == 403 else _SPENT_RATE_MARKERS
    return any(m in text for m in markers)


def _is_limit_refusal(resp: httpx.Response) -> bool:
    """A 429 whose body says a usage limit or reserved capacity, the kind the
    limit hold escalates on."""
    text = (resp.text or "").lower()
    return resp.status_code == 429 and any(m in text for m in _LIMIT_REFUSAL_MARKERS)


def _next_limit_hold(config: BackendConfig, resp: httpx.Response) -> float | None:
    """Seconds to hold the provider after this answer, or None for no new hold.

    A limit refusal from a provider that is not already held steps the hold up
    (2, 4, ... 30 minutes); one from a call that was in flight when another
    opened the hold adds nothing. A success clears the escalation and the
    hold, locally and in the shared store."""
    pool = _backend_pool(config.backend, config.model)
    key = (config.backend.value, pool)
    now = time.time()
    if resp.is_success:
        with _provider_breaker_lock:
            was_held = _limit_hold_level.pop(key, None) is not None
            if was_held:
                _provider_blocked_until.pop(key, None)
        if was_held:
            shared_benches.clear("provider", *key)
            shared_benches.clear("limit", *key)
        return None
    if not _is_limit_refusal(resp):
        return None
    held_until = shared_benches.blocked_until("provider", *key)
    with _provider_breaker_lock:
        if now < max(_provider_blocked_until.get(key, 0.0), held_until):
            return None
        level = _limit_hold_level.get(key, 0)
        _limit_hold_level[key] = level + 1
    return min(_LIMIT_HOLD_BASE_SECONDS * 2 ** level, _LIMIT_HOLD_MAX_SECONDS)


def _limit_hold_error() -> str:
    """A `partial review:` error naming a provider held by the limit hold, so
    a review made during one ends degraded; empty when none is held."""
    now = time.time()
    held = sorted(
        f"{backend.value}:{pool}"
        for backend in _LIMIT_HOLD_BACKENDS
        for pool in _PROVIDER_POOLS
        if shared_benches.blocked_until("limit", backend.value, pool) > now
        or _limit_hold_active((backend.value, pool), now)
    )
    if not held:
        return ""
    return f"partial review: provider held after repeated usage limit refusals ({', '.join(held)})"


def _limit_hold_active(key: tuple[str, str], now: float) -> bool:
    with _provider_breaker_lock:
        return key in _limit_hold_level and _provider_blocked_until.get(key, 0.0) > now


def _note_provider_response(config: BackendConfig, resp: httpx.Response) -> None:
    """Open the provider breaker on a 401, a 402, a spent-limit refusal, or a
    limit refusal (the escalating hold), and clear that hold on a success."""
    pool = _backend_pool(config.backend, config.model)
    if config.backend in _BENCH_CLOUD_BACKENDS:
        # Same default (an hour) and cap (a week) as the OpenCode Go breaker.
        if resp.status_code == 401:
            _open_provider_breaker(config, resp, _PROVIDER_POOLS, time.time() + _retry_after_seconds(resp))
            return
        if _is_spent_quota_refusal(resp):
            _open_provider_breaker(config, resp, (pool,), time.time() + _retry_after_seconds(resp))
            return
    if config.backend not in _LIMIT_HOLD_BACKENDS:
        return
    seconds = _next_limit_hold(config, resp)
    if seconds is not None:
        until = time.time() + seconds
        _open_provider_breaker(config, resp, (pool,), until)
        shared_benches.publish("limit", config.backend.value, pool, until)


def _open_provider_breaker(
    config: BackendConfig, resp: httpx.Response, pools: tuple[str, ...], until: float,
) -> None:
    seconds = until - time.time()
    with _provider_breaker_lock:
        for pool in pools:
            key = (config.backend.value, pool)
            _provider_blocked_until[key] = max(_provider_blocked_until.get(key, 0.0), until)
    for pool in pools:
        shared_benches.publish("provider", config.backend.value, pool, until)
    log.warning(
        "llm_provider_circuit_open",
        extra={
            "backend": config.backend.value, "model": config.model,
            "status": resp.status_code, "pools": list(pools), "block_seconds": seconds,
        },
    )


def _refuse_unusable_backend(config: BackendConfig, *, public: bool) -> None:
    """`_call_backend`'s guard: raise before any network call to a backend
    that is switched off or whose breaker is open."""
    if not _backend_enabled(config.backend, config.model):
        raise BackendDisabledError(
            f"{config.backend.value} is switched off (GRUG_DISABLED_BACKENDS)"
        )
    if (
        config.backend == Backend.OPENCODE_GO
        and config.model not in _OPENCODE_GO_PRIVATE_SAFE_MODELS
        and not public
    ):
        raise BackendDisabledError(
            f"{config.model} has no verified data terms for private code"
        )
    if config.backend in _BENCH_CLOUD_BACKENDS and not _provider_available(
        config.backend, config.model,
    ):
        raise BackendDisabledError(
            f"{config.backend.value} provider breaker open after an auth or quota refusal"
        )
    if not _go_allowance_available(config.backend, config.model):
        raise OpencodeGoCircuitOpenError("opencode Go usage limit: breaker open")


# Slow bench. A model that is merely slow (not withdrawn) times out on large
# cohorts and burns its whole timeout before the next tier starts. Three
# httpx timeouts within ten minutes bench the (backend, model) pair for ten
# minutes, doubling per repeat up to an hour; any successful response clears
# it at once. Separate from the 4xx bench above: a timeout never counts
# there and a 4xx never counts here. Only cloud backends are covered (the
# Cave is never slow-benched), and the chain never drops its last usable
# cloud tier (`_cloud_chain_tiers`).
_SLOW_BENCH_FAILURE_THRESHOLD = 3
_SLOW_BENCH_WINDOW_SECONDS = 600.0
_SLOW_BENCH_BASE_SECONDS = 600.0
_SLOW_BENCH_MAX_SECONDS = 3600.0
_slow_bench_state: dict[tuple[str, str], _BenchEntry] = {}


def _model_is_slow_benched(backend: str, model: str, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    with _bench_lock:
        entry = _slow_bench_state.get((backend, model))
        local = entry.benched_until if entry is not None else 0.0
    return current < max(local, shared_benches.blocked_until("slow", backend, model))


def _record_model_timeout(backend: str, model: str, now: float) -> float | None:
    """Count one timeout; return the bench duration when it trips the bench."""
    with _bench_lock:
        entry = _slow_bench_state.setdefault(
            (backend, model), _BenchEntry(next_seconds=_SLOW_BENCH_BASE_SECONDS),
        )
        if now < entry.benched_until:
            return None
        entry.failures = [t for t in entry.failures if now - t < _SLOW_BENCH_WINDOW_SECONDS]
        entry.failures.append(now)
        if len(entry.failures) < _SLOW_BENCH_FAILURE_THRESHOLD:
            return None
        seconds = entry.next_seconds
        entry.benched_until = now + seconds
        entry.next_seconds = min(seconds * 2, _SLOW_BENCH_MAX_SECONDS)
        entry.failures = []
        return seconds


def _announce_model_slow_benched(backend: str, model: str, seconds: float) -> None:
    log.warning(
        "llm_model_slow_benched",
        extra={"backend": backend, "model": model, "bench_seconds": seconds},
    )
    try:
        from observability import emit_count  # type: ignore  # late: webhook-image only

        emit_count("grug.elder.model_slow_benched", 1, tags={"backend": backend, "model": model})
    except Exception as e:  # noqa: BLE001 - telemetry must never break the review
        log.debug("model_slow_benched_metric_failed", extra={"kind": type(e).__name__})


def _note_model_timeout(backend: Backend, model: str, exc: BaseException) -> None:
    """Feed one transport failure to the slow bench (timeouts only)."""
    if backend not in _BENCH_CLOUD_BACKENDS or not isinstance(exc, httpx.TimeoutException):
        return
    key = (backend.value, model)
    now = time.time()
    seconds = _record_model_timeout(*key, now)
    if seconds is not None:
        shared_benches.publish("slow", *key, now + seconds)
        _announce_model_slow_benched(*key, seconds)


def _usable_tiers(
    candidates: "list[BackendConfig | None]", *, skip_slow: bool,
) -> list[BackendConfig]:
    tiers: list[BackendConfig] = []
    for tier in candidates:
        if tier is not None and _tier_is_usable(tier, tiers, skip_slow=skip_slow):
            tiers.append(tier)
    return tiers


def _slow_bench_expiry(tier: BackendConfig) -> float:
    key = (tier.backend.value, tier.model)
    with _bench_lock:
        entry = _slow_bench_state.get(key)
        local = entry.benched_until if entry is not None else 0.0
    return max(local, shared_benches.blocked_until("slow", *key))


def _tier_is_usable(
    tier: BackendConfig, chosen: list[BackendConfig], *, skip_slow: bool = True,
) -> bool:
    """A tier runs unless it duplicates one already chosen, is switched off
    or behind an open provider breaker (`backend_usable`), is benched (4xx
    bench, or slow bench when `skip_slow`), or is a paid OpenCode Go model
    while the usage-limit breaker is open."""
    if any((c.backend, c.model) == (tier.backend, tier.model) for c in chosen):
        return False
    if not backend_usable(tier.backend, tier.model):
        return False
    if _model_is_benched(tier.backend.value, tier.model):
        return False
    if skip_slow and _model_is_slow_benched(tier.backend.value, tier.model):
        return False
    return _go_allowance_available(tier.backend, tier.model)


def _go_allowance_available(backend: Backend, model: str) -> bool:
    return (
        backend != Backend.OPENCODE_GO
        or _opencode_go_model_is_free(model)
        or _opencode_go_available()
    )


def _backend_error_message(resp: httpx.Response) -> str:
    """The provider's own error message from a non-2xx body, redacted and
    capped, or "" when the body carries none. OpenAI-compatible providers
    answer `{"error": {"message": ...}}`; a bare string `error` or a
    top-level `message` is accepted too."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
    elif isinstance(err, str):
        msg = err
    else:
        msg = body.get("message")
    return _redact_secrets(msg)[:200] if isinstance(msg, str) else ""


def cloud_tiers(candidates: list[BackendConfig | None]) -> list[BackendConfig]:
    """Keep the earliest recovering slow tier when all usable tiers are slow."""
    tiers = _usable_tiers(candidates, skip_slow=True)
    if tiers:
        return tiers
    return sorted(_usable_tiers(candidates, skip_slow=False), key=_slow_bench_expiry)[:1]


def subscription_status(config: BackendConfig | None) -> Literal["unavailable", "capped", "ready"]:
    if config is None or not backend_usable(config.backend, config.model):
        return "unavailable"
    return "capped" if _is_subscription_cap_reached() else "ready"


def prepare_call(
    config: BackendConfig, *, public: bool, cancel_event: threading.Event | None,
) -> None:
    """Check admission and reserve allowance before credentials or network I/O."""
    _refuse_unusable_backend(config, public=public)
    if config.backend == Backend.OPENROUTER and is_free_tier_model(config.model):
        outcome = acquire_free_tier_slot(config.model, cancel_event=cancel_event)
        if not outcome.admitted:
            raise RateLimitTimeoutError(
                "openrouter free-tier limiter exhausted its queue wait "
                f"({outcome.waited_seconds:.1f}s; minute={outcome.minute_count}/"
                f"{outcome.minute_limit}, day={outcome.day_count}/{outcome.day_limit}) "
                f"for model {config.model!r}"
            )
    _reserve_opencode_go_call(config)


def note_response(config: BackendConfig, resp: httpx.Response) -> None:
    """Update the relevant breaker, pacing window and bench from one reply."""
    if config.backend == Backend.OPENCODE_GO:
        _log_opencode_go_usage(config.model, resp)
        _note_opencode_go_response(resp)
        if _opencode_go_model_is_free(config.model):
            _opencode_go_free_gate_get().note_response(config.model, resp)
    _note_provider_response(config, resp)
    _note_model_response(config, resp)


def interactive_go_available() -> bool:
    """The interactive Go fallback requires a configured key and paid allowance."""
    return bool(os.getenv("GRUG_OPENCODE_GO_API_KEY_SSM", "").strip()) and _opencode_go_available()


def second_lineage_share_spent() -> bool:
    """Keep half of the subscription cap available for the primary review."""
    return _subscription_calls_today() * 2 >= _subscription_daily_cap()


def provider_breaker_open(backend: Backend, model: str) -> bool:
    """Name a provider breaker only when that provider is enabled."""
    return _backend_enabled(backend, model) and not _provider_available(backend, model)
