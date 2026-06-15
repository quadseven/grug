# WEBHOOK-ONLY (NOT mirrored): operator CLI shipped in the grug-webhook image.
"""Manual fallback CLI: replay missed GitHub App webhook deliveries (#407).

The grug-poller CronJob replays automatically every tick; this is the
on-demand operator fallback for "replay missed deliveries since T". It runs
the SAME `delivery_replay.replay_since()` the poller runs - no second
implementation. Run it where the App SSM env is already wired (the webhook
pods), so no local AWS cred setup is needed:

    kubectl -n grug exec deploy/grug-webhook -- python replay_deliveries.py --hours 6
    kubectl -n grug exec deploy/grug-webhook -- python replay_deliveries.py --since 2026-06-14T20:00:00Z

Exit code is non-zero if any redeliver attempt errored (partial failure), so a
wrapping runbook command surfaces it.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import delivery_replay


def _since_iso(*, since: str | None = None, hours: float | None = None, now=None) -> str:
    """Resolve the replay-window start to an ISO-8601 'Z' instant. Pure, with
    an injectable `now` for tests. `since` wins if given; else now - hours."""
    if since:
        return since
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="Replay missed GitHub App webhook deliveries (#407)"
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--since", help="ISO-8601 UTC instant, e.g. 2026-06-14T20:00:00Z"
    )
    g.add_argument(
        "--hours", type=float, help="replay deliveries from the last N hours"
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    since = _since_iso(since=args.since, hours=args.hours)
    report = delivery_replay.replay_since(since)
    print(
        f"replay since {since}: scanned={report.scanned} "
        f"failed_guids={report.failed_guids} redelivered={report.redelivered} "
        f"errors={report.errors}"
    )
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
