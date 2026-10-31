"""How often each recorded comment's reactions are re-checked.

The scheduled poll used to fetch the reactions of EVERY comment record (30-day
TTL) on EVERY 15-minute run: about 1,000 GitHub calls a run, 4,100 an hour,
70% of the installation's 6,200/hour budget before any review traffic. On
2026-10-05 a burst of deploys on top of that exhausted the budget twice, and
GitHub answered 403 to every read and write until the window reset; Chief
could not publish and PRs were merge-blocked.

Reactions arrive within hours of a comment and almost never after a week, so
a record's poll rate decays with its age. The schedule is stateless: a record
is due on the runs where `(comment_id + tick) % period == 0`, which spreads
the load evenly without storing a last-polled time.

    age < 6 hours    every run (15 min)
    6 hours to 1 day hourly
    1 to 3 days      every 6 hours
    3 to 14 days     daily
    older            never

The first schedule polled every comment under a day old on every run and kept
polling old ones daily. That held while reviews were few. By 2026-10-31 there
were 3,300 live records and a run spent 232 calls to submit nothing, so a
comment now stops being polled after two weeks (grug#1539).

A record of unknown age is treated as fresh (polled every run).
"""

from __future__ import annotations

RECORD_TTL_DAYS = 30            # must match adapters.install_store's record TTL
TICK_SECONDS = 15 * 60          # the poller CronJob cadence
_DAY = 86400

# (max age in days, period in ticks). Past the last bucket a record is not polled.
_SCHEDULE: tuple[tuple[float, int], ...] = (
    (0.25, 1),
    (1.0, 4),
    (3.0, 24),
    (14.0, 96),
)


def tick_due(key: int, period: int, now_epoch: float) -> bool:
    """Whether `key` is due on this run of a `period`-tick schedule. Keys spread
    evenly across runs, with no last-run time to store."""
    return (int(key) + int(now_epoch // TICK_SECONDS)) % period == 0


def _period_ticks(age_days: float) -> int | None:
    for max_age, period in _SCHEDULE:
        if age_days < max_age:
            return period
    return None


def reaction_poll_due(record, now_epoch: float) -> bool:
    """Whether `record`'s reactions should be fetched on this poller run."""
    ttl = record.get("ttl")
    if not isinstance(ttl, (int, float)):
        return True
    created = float(ttl) - RECORD_TTL_DAYS * _DAY
    age_days = max(0.0, (now_epoch - created) / _DAY)
    period = _period_ticks(age_days)
    if period is None:
        return False
    return tick_due(int(record["comment_id"]), period, now_epoch)
