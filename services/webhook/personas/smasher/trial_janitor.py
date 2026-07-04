"""Trial orphan janitor (#469, ADR-0013, codex peer-review PR #494). WEBHOOK-ONLY.

The launcher cleans up its Jobs/PVC/Secret in a `finally`, but a webhook/consumer
POD CRASH mid-run skips that path, leaving a per-Trial PVC (which holds a private
repo checkout) and token Secret behind. Jobs self-clean via
`ttlSecondsAfterFinished`, but PVCs and Secrets do not. This CronJob reaps any
`app: grug-trial` PVC / Secret / Job in the grug-trial namespace older than a
cutoff (well past the max Trial wall-clock budget), so a crashed launcher can't
leave private checkout data or credentials lying around indefinitely.

`reap_orphans` is the injectable, pure-logic core (clock + cluster injected) so
the age math is unit-tested without a live API server.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Protocol

log = logging.getLogger("grug.smasher.trial_janitor")

# Default reap age: 30 min. The total Trial budget is ~10 min, so anything this
# old is definitively orphaned (no legitimate Trial runs that long).
_DEFAULT_MAX_AGE_SECONDS = 1800


class JanitorCluster(Protocol):
    def list_orphans(self) -> list[tuple[str, str, str]]:
        """(kind, name, creation_timestamp_rfc3339) for every app=grug-trial
        PVC / Secret / Job in the namespace."""
        ...

    def delete(self, kind: str, name: str) -> None: ...


def reap_orphans(
    *, cluster: JanitorCluster, now_epoch: float, max_age_seconds: int,
) -> dict[str, int]:
    """Delete every app=grug-trial resource older than `max_age_seconds`. Never
    raises: a per-resource delete failure is logged and skipped. An unparseable
    creation timestamp is treated as NOT-old (kept) - we never delete something
    whose age we can't determine."""
    reaped = {"pvc": 0, "secret": 0, "job": 0}
    try:
        orphans = cluster.list_orphans()
    except Exception as e:  # noqa: BLE001 — a list failure degrades to a no-op run
        log.warning("trial_janitor_list_failed", extra={"kind": type(e).__name__})
        return reaped
    for kind, name, created in orphans:
        created_epoch = _parse_ts(created)
        if created_epoch is None:
            continue  # unknown age -> keep (never delete blind)
        if now_epoch - created_epoch <= max_age_seconds:
            continue  # still within a plausible live Trial window
        try:
            cluster.delete(kind, name)
            reaped[kind] = reaped.get(kind, 0) + 1
            log.info("trial_janitor_reaped", extra={"kind": kind, "resource": name})
        except Exception as e:  # noqa: BLE001 — one bad delete doesn't stop the sweep
            log.warning(
                "trial_janitor_delete_failed",
                extra={"kind": kind, "resource": name, "error": type(e).__name__},
            )
    return reaped


def _parse_ts(rfc3339: str) -> float | None:
    """Parse a k8s creationTimestamp (`2026-07-04T12:00:00Z`) to epoch seconds,
    or None if it doesn't parse."""
    try:
        dt = datetime.strptime(rfc3339, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    from personas.smasher.trial_runner import build_janitor_cluster

    cluster = build_janitor_cluster()
    if cluster is None:
        log.error("trial_janitor_no_credentials")
        return 0  # nothing to do without cluster creds; never crash-loop the CronJob
    max_age = _int_env("GRUG_TRIAL_JANITOR_MAX_AGE_SECONDS", _DEFAULT_MAX_AGE_SECONDS)
    result = reap_orphans(cluster=cluster, now_epoch=time.time(), max_age_seconds=max_age)
    log.info("trial_janitor_done", extra=result)
    return 0


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


if __name__ == "__main__":
    raise SystemExit(main())
