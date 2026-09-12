"""Warder deploy-gate: Datadog SLO/monitor state (grug#533, epic #824).

The roster sells Warder gating a release on staging/SLO health; the
release-tracer (`dispatch.py`, #471) already owns the changelog+semver
half. This adds the missing half: on the same merged-PR release event,
query the repo's configured DD monitor and fold a deploy-gate verdict
into that SAME check-run pass (not a second check-run - see `dispatch.py`
for why: a second check-run under the "warder" persona key would both
escape `check_run_reconciler`'s per-persona sweep, which iterates one
`check_run_name` per registry entry, and clobber the first `activity_log`
row, which upserts on (persona, head_sha)).

EXPLICIT ALLOW: the repo -> DD monitor-id mapping lives in one operator-
managed SSM param (`GRUG_WARDER_SLO_MAP_SSM`, JSON
{"owner/repo": monitor_id}), same shape as Omen's service map (#470). No
mapping = ZERO DD calls = the release-tracer's original behavior
unchanged (acceptance: advisory default). Reuses Omen's DD-key gate
(`get_dd_api_key`/`get_dd_app_key`) - no new secret surface.

FAIL-SAFE contract: a query failure (creds absent, DD unreachable,
malformed response) degrades to `None` - the gate stays silent and the
release-tracer's own verdict is unaffected. A checker error must never
itself block a release; only a genuine, successfully-read breach can.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.warder.slo_gate")

_CALL_TIMEOUT = 10
# DD monitor `overall_state` values that count as healthy. Everything else
# (Alert, Warn, No Data, ...) reads as "in breach" per the issue's binary
# framing - a monitor that cannot report OK is not a release green light.
_HEALTHY_STATES = frozenset({"OK"})


def _dd_site() -> str:
    return os.getenv("GRUG_DD_SITE", "datadoghq.com").strip()


def _slo_map_from_json(raw: str) -> dict[str, int]:
    """Parse the operator's mapping JSON; {} on any malformation (logged
    by the loader). Split out for testability, mirrors
    `omen._service_map_from_json` but with int-valued monitor ids."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in parsed.items():
        if not isinstance(k, str):
            continue
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def query_monitor_state(monitor_id: int, api_key: str, app_key: str) -> str | None:
    """The DD monitor's `overall_state` (e.g. "OK", "Alert", "Warn"), or
    None on any transport/parse failure - caller degrades (never raises).
    """
    try:
        resp = httpx.get(
            f"https://api.{_dd_site()}/api/v1/monitor/{int(monitor_id)}",
            headers={"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key},
            timeout=_CALL_TIMEOUT,
        )
        resp.raise_for_status()
        state = (resp.json() or {}).get("overall_state")
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
        log.info(
            "warder_slo_gate_degraded",
            extra={"stage": "query", "monitor_id": monitor_id, "kind": type(e).__name__},
        )
        return None
    if not isinstance(state, str) or not state:
        log.info(
            "warder_slo_gate_degraded",
            extra={"stage": "malformed_response", "monitor_id": monitor_id},
        )
        return None
    return state


def is_healthy(state: str) -> bool:
    """Pure: whether a DD `overall_state` value counts as gate-healthy."""
    return state in _HEALTHY_STATES
