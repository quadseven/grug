"""Elder's blocking relaxes to advisory during a sustained backend outage (ADR-0027).

ADR-0023 made a degraded Elder review fail closed in BLOCK mode. That held
every merge for hours when the review backends were down for a long time.
The operator's decision: do not block while the backends are down, and
restore blocking by itself when they are back.

The rule, in one place:

  * Every Elder review result is classified: `healthy` (a real review),
    `outage` (a degraded review caused by failing backends) or `other`
    (anything else: a partial review of a huge PR, `no_diff`).
  * The install keeps one streak of consecutive `outage` results. A `healthy`
    result ends it; an `other` result leaves it alone.
  * A BLOCK-mode outage result whose streak is at least `window` old (default
    20 minutes) publishes an ADVISORY check instead of a failure: conclusion
    `neutral`, titled `ADVISORY_TITLE`, which no real "Elder clear" shares.
  * Nothing to restore: the next healthy review ends the streak, so the very
    next degraded one starts a fresh, blocking, streak.

`classify`, `advance` and `decide` are pure (the clock and the backend health
are arguments). The streak itself lives in `shared_benches` (the store Elder
already uses for its backend benches and breakers) when it is on, and in
process memory otherwise. Guard and Smasher do not come through here.

Shared encoding. `shared_benches` keeps "blocked until" times that expire on
their own, so the streak is written as two such entries:
  * `last`: until = last outage result + gap. Expiring means "no outage result
    for a gap": the streak is over, with no one needed to clear it.
  * `since`: until = _SINCE_BASE - streak start. The store keeps the later
    `until`, so the earliest start wins, and the value stays in the future.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import shared_benches

ADVISORY_TITLE = "Elder advisory: review backends down"

DEFAULT_WINDOW_MINUTES = 20.0
DEFAULT_GAP_MINUTES = 60.0
_SHOWN_FAILURES = 5

# Degraded reasons that mean no backend gave a usable answer for the diff.
_OUTAGE_REASONS = frozenset(
    {"all_failed", "parse_failed", "subscription_limit_reached", "low_confidence_review"}
)
_PARTIAL = "partial_review"

_SINCE_BASE = 4_000_000_000.0
_KEY = "elder"

Kind = Literal["healthy", "outage", "other"]


@dataclass(frozen=True)
class OutageSettings:
    enabled: bool = True
    window_s: float = DEFAULT_WINDOW_MINUTES * 60
    gap_s: float = DEFAULT_GAP_MINUTES * 60


def _minutes(raw: str | None, default: float) -> float:
    try:
        value = float((raw or "").strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def settings_from_env(env: Mapping[str, str] | None = None) -> OutageSettings:
    """`GRUG_ELDER_OUTAGE_ADVISORY` (default on; 0/false/no/off restores
    ADR-0023 exactly), `GRUG_ELDER_OUTAGE_MINUTES` (20),
    `GRUG_ELDER_OUTAGE_GAP_MINUTES` (60: no outage result for this long ends
    the streak)."""
    source = os.environ if env is None else env
    switch = source.get("GRUG_ELDER_OUTAGE_ADVISORY", "").strip().lower()
    return OutageSettings(
        enabled=switch not in ("0", "false", "no", "off"),
        window_s=_minutes(
            source.get("GRUG_ELDER_OUTAGE_MINUTES"), DEFAULT_WINDOW_MINUTES,
        ) * 60,
        gap_s=_minutes(
            source.get("GRUG_ELDER_OUTAGE_GAP_MINUTES"), DEFAULT_GAP_MINUTES,
        ) * 60,
    )


@dataclass(frozen=True)
class Streak:
    since: float
    last: float


def classify(reason: str | None, coverage: Any = None) -> Kind:
    """What one Elder result says about the review backends."""
    if not reason:
        return "healthy"
    if reason in _OUTAGE_REASONS:
        return "outage"
    if reason == _PARTIAL and coverage is not None:
        # A cohort that RAN and failed is a backend failure. Cohorts never
        # attempted (budget) or capped are a big diff, not an outage.
        unattempted = set(getattr(coverage, "unattempted_cohorts", ()) or ())
        ran_and_failed = [
            c for c in (getattr(coverage, "failed_cohorts", ()) or ())
            if c not in unattempted
        ]
        if ran_and_failed:
            return "outage"
    return "other"


def advance(prev: Streak | None, kind: Kind, now: float, gap_s: float) -> Streak | None:
    """The streak after one more result."""
    if kind == "healthy":
        return None
    if prev is not None and now - prev.last > gap_s:
        prev = None
    if kind == "other":
        return prev
    return Streak(since=prev.since if prev else now, last=now)


@dataclass(frozen=True)
class OutageVerdict:
    advisory: bool
    since: float | None = None
    failed: tuple[str, ...] = ()


def decide(
    *, settings: OutageSettings, blocking: bool | None, kind: Kind,
    streak: Streak | None, now: float, failed: Sequence[str] = (),
) -> OutageVerdict:
    """Advisory only for a BLOCK-mode outage result inside a streak that has
    lasted at least the window. Everything else keeps ADR-0023."""
    if (
        settings.enabled
        and blocking is True
        and kind == "outage"
        and streak is not None
        and now - streak.since >= settings.window_s
    ):
        return OutageVerdict(True, streak.since, tuple(failed))
    return OutageVerdict(False)


def advisory_summary(verdict: OutageVerdict, summary: str, now: float | None = None) -> str:
    """The advisory's explanation, ahead of the review's own summary."""
    when = datetime.fromtimestamp(verdict.since or 0, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC",
    )
    failed = (
        "Failed: " + "; ".join(verdict.failed[:_SHOWN_FAILURES]) + "."
        if verdict.failed else "No backend gave a usable answer."
    )
    return (
        f"## {ADVISORY_TITLE}\n\n"
        "Elder did NOT review this change. Its review backends have been "
        f"failing since {when}, so this check is advisory (`neutral`) and is "
        "not an \"Elder clear\". "
        f"{failed}\n\n"
        "Blocking returns by itself once a review completes. Push, or "
        "comment `/grug improve`, to get a fresh review.\n\n"
        f"{summary}"
    )


# -- the streak store -------------------------------------------------------

_lock = threading.Lock()
_local: Streak | None = None


def reset_for_tests() -> None:
    global _local
    with _lock:
        _local = None


def _read(gap_s: float, now: float) -> Streak | None:
    if shared_benches.enabled():
        last_until = shared_benches.blocked_until("outage", "last", _KEY)
        since_enc = shared_benches.blocked_until("outage", "since", _KEY)
        if not last_until or not since_enc:
            return None
        return Streak(since=_SINCE_BASE - since_enc, last=last_until - gap_s)
    with _lock:
        return _local


def _write(prev: Streak | None, new: Streak | None, gap_s: float) -> None:
    global _local
    if shared_benches.enabled():
        if new is None:
            shared_benches.clear("outage", "last", _KEY)
            shared_benches.clear("outage", "since", _KEY)
            return
        if prev is None or prev.since != new.since:
            shared_benches.clear("outage", "since", _KEY)
        shared_benches.publish("outage", "since", _KEY, _SINCE_BASE - new.since)
        shared_benches.publish("outage", "last", _KEY, new.last + gap_s)
        return
    with _lock:
        _local = new


def observe(
    reason: str | None, coverage: Any, *, blocking: bool | None,
    record: bool = True, settings: OutageSettings | None = None,
    now: float | None = None, failed: Sequence[str] | None = None,
) -> OutageVerdict:
    """Classify one Elder result, move the streak (when `record`), decide.

    Never raises: a store problem leaves the review failing closed, as
    ADR-0023 says. `failed` overrides the discovered backend names (tests)."""
    cfg = settings or settings_from_env()
    kind = classify(reason, coverage)
    try:
        stamp = time.time() if now is None else now
        prev = _read(cfg.gap_s, stamp)
        streak = advance(prev, kind, stamp, cfg.gap_s) if record else (
            advance(prev, "other", stamp, cfg.gap_s)
        )
        if record:
            _write(prev, streak, cfg.gap_s)
        names = failed if failed is not None else failed_backends(coverage)
        return decide(
            settings=cfg, blocking=blocking, kind=kind, streak=streak,
            now=stamp, failed=names,
        )
    except Exception:  # noqa: BLE001 - the streak store must never break a review
        return OutageVerdict(False)


def failed_backends(coverage: Any) -> tuple[str, ...]:
    """Which backends failed: breaker-open backends, then the models this
    review tried and lost."""
    names: list[str] = []
    try:
        import llm_client as lc

        for backend in lc.Backend:
            config = lc._BACKEND_CONFIGS.get(backend)  # noqa: SLF001
            if (
                config is not None
                and lc._backend_enabled(backend, config.model)  # noqa: SLF001
                and not lc._provider_available(backend, config.model)  # noqa: SLF001
            ):
                names.append(f"{backend.value} (breaker open)")
    except Exception:  # noqa: BLE001 - naming is best effort
        pass
    names.extend(getattr(coverage, "failed_models", ()) or ())
    return tuple(dict.fromkeys(names))
