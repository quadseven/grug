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

    age < 1 day      every run (15 min)
    1 to 3 days      hourly
    3 to 14 days     every 6 hours
    older            daily

A record of unknown age is treated as fresh (polled every run).
"""

from __future__ import annotations

RECORD_TTL_DAYS = 30            # must match adapters.install_store's record TTL
TICK_SECONDS = 15 * 60          # the poller CronJob cadence
_DAY = 86400

# (max age in days, period in ticks); the last bucket has no upper bound.
_SCHEDULE: tuple[tuple[float, int], ...] = (
    (1.0, 1),
    (3.0, 4),
    (14.0, 24),
)
_OLDEST_PERIOD = 96


def _period_ticks(age_days: float) -> int:
    for max_age, period in _SCHEDULE:
        if age_days < max_age:
            return period
    return _OLDEST_PERIOD


def reaction_poll_due(record, now_epoch: float) -> bool:
    """Whether `record`'s reactions should be fetched on this poller run."""
    ttl = record.get("ttl")
    if not isinstance(ttl, (int, float)):
        return True
    created = float(ttl) - RECORD_TTL_DAYS * _DAY
    age_days = max(0.0, (now_epoch - created) / _DAY)
    period = _period_ticks(age_days)
    if period == 1:
        return True
    tick = int(now_epoch // TICK_SECONDS)
    return (int(record["comment_id"]) + tick) % period == 0
