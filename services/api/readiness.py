# MIRRORED — sibling at services/webhook/readiness.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Dependency-aware readiness for /readyz (#404).

`/readyz` must mean "this pod can do its job", not just "the process is up".
A pod whose AWS credentials are broken (the 2026-06-14 deleted-key incident)
still answered /livez 200, so k8s kept routing to it and a bad rollout
completed. This module probes the CRITICAL dependencies - SSM+KMS (a
SecureString read, which exercises AWS auth + KMS decrypt in one call) and
Postgres (SELECT 1) - and the /readyz handlers return 503 when any is
unreachable.

Effect: a dependency-broken pod fails readiness, so k8s stops routing to it
AND a rollout of broken pods never completes (the last-good ReplicaSet keeps
serving). A silent cluster-wide outage becomes a self-limiting deploy.

Results are TTL-cached (a few seconds) so frequent kubelet probes don't hammer
the backends, and the check FAILS CLOSED: any unexpected error -> not ready
(never falsely ready).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import boto3
import psycopg
from botocore.config import Config

log = logging.getLogger("grug.readiness")

_TTL_SECONDS = 5.0
# Module-level single-slot cache. Mutated under Mangum/uvicorn's effectively
# serial probe cadence; a benign duplicate check at a TTL boundary is fine.
_cache: dict = {"at": -1.0e9, "report": None}

# A readiness probe must fail FAST. A fast not-ready that recovers on the next
# TTL is fine, but a SLOW probe that blows the kubelet timeoutSeconds drops the
# replica and turns a blip / load-spike into an outage. So the probe's SSM
# client uses tight timeouts + no retries (botocore defaults are ~60s + several
# retries), and it is module-scoped for warm reuse (matches the cf_auth /
# secrets_loader module-client pattern).
_SSM_PROBE_CONFIG = Config(
    connect_timeout=1, read_timeout=2, retries={"max_attempts": 1, "mode": "standard"}
)
_ssm = boto3.client("ssm", config=_SSM_PROBE_CONFIG)
# Bound the Postgres reachability probe well under the kubelet probe timeout.
_PG_CONNECT_TIMEOUT_S = 2


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    deps: dict  # dependency name -> reachable bool


def _check_ssm_kms() -> None:
    """SecureString read: exercises AWS auth (catches a deleted/invalid key)
    AND KMS decrypt. Probes a param the pod already reads, so no extra IAM."""
    name = os.environ.get("GRUG_READYZ_SSM_PROBE") or os.environ["GITHUB_APP_ID_SSM"]
    _ssm.get_parameter(Name=name, WithDecryption=True)


def _check_postgres() -> None:
    """Probe DB REACHABILITY with a SEPARATE short-lived connection, NOT the
    request pool: a pod serving at max pool is busy, not unready, so readiness
    must not be held hostage by an exhausted/contended pool. `connect_timeout`
    bounds it under the kubelet probe timeout."""
    url = os.environ["GRUG_DATABASE_URL"]
    with psycopg.connect(url, connect_timeout=_PG_CONNECT_TIMEOUT_S) as conn:
        conn.execute("SELECT 1")


def check_readiness(*, now=time.monotonic) -> ReadinessReport:
    """Probe the critical dependencies (TTL-cached, fail-closed). Returns a
    ReadinessReport; never raises."""
    t = now()
    cached = _cache["report"]
    if cached is not None and t - _cache["at"] < _TTL_SECONDS:
        return cached
    # Built per call so tests can monkeypatch the module-level check fns.
    checks = {"ssm_kms": _check_ssm_kms, "postgres": _check_postgres}
    deps: dict[str, bool] = {}
    for name, fn in checks.items():
        try:
            fn()
            deps[name] = True
        except Exception as e:  # noqa: BLE001 - fail CLOSED on ANY error
            log.warning(
                "readyz_dependency_unreachable",
                extra={"dependency": name, "err": str(e)},
            )
            deps[name] = False
    report = ReadinessReport(ready=all(deps.values()), deps=deps)
    _cache["at"] = t
    _cache["report"] = report
    return report


def _reset_cache() -> None:
    """Test hook - clears the TTL cache between tests."""
    _cache["at"] = -1.0e9
    _cache["report"] = None
