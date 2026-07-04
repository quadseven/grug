# MIRRORED — sibling at services/api/personas/code_reviewer/omen.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Grug Omen - production-signal fusion for the Elder review (#470,
epic #464 slice 6 = PRD #346 P2.1, the Seer-class capability tracer).

Grug reviews code blind to production; we own the whole Datadog estate.
Omen queries DD error logs for the files a diff touches and injects a
compact hot-path summary into the review prompt ("dispatcher.py errored
47x in 7d"), paired with the `hot-path-unguarded` rule - findings
grounded in what the code DOES in prod, the differentiator a SaaS
reviewer cannot copy without our data.

EXPLICIT ALLOW: the repo -> DD service mapping lives in one operator-
managed SSM param (`GRUG_OMEN_SERVICE_MAP_SSM`, JSON
{"owner/repo": "service"}). No mapping = ZERO DD calls = byte-identical
review (acceptance #2); DD creds absent/invalid = same. A mapping is
never inferred, so DD data can never leak into a repo that does not own
the service.

FAIL-SAFE contract: every error path degrades to None (today's review),
logged `omen_degraded`. Budgets mirror cross_file's: per-call timeout
clamped to a global wall-clock deadline, capped file count.
"""

from __future__ import annotations

import json
import logging
import os
import time
from urllib.parse import quote

import httpx

from personas.code_reviewer.diff_parser import DiffHunk
from secrets_loader import get_dd_api_key, get_dd_app_key, get_omen_service_map

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.omen")

_MAX_FILES = 5
_WINDOW = "now-7d"
_CALL_TIMEOUT = 10
_TOTAL_BUDGET_SECONDS = 8.0
# Below this count the signal is noise, not an omen.
_MIN_ERROR_COUNT = 5


def _dd_site() -> str:
    return os.getenv("GRUG_DD_SITE", "datadoghq.com").strip()


def _error_count(
    api_key: str, app_key: str, service: str, basename: str, deadline: float,
) -> int | None:
    """Count of status:error logs mentioning `basename` for `service` in
    the window. None on failure (caller degrades)."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    resp = httpx.post(
        f"https://api.{_dd_site()}/api/v2/logs/analytics/aggregate",
        headers={
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
            "Content-Type": "application/json",
        },
        json={
            "compute": [{"aggregation": "count"}],
            "filter": {
                "query": f'service:{service} status:error "{basename}"',
                "from": _WINDOW,
                "to": "now",
            },
        },
        timeout=min(_CALL_TIMEOUT, remaining),
    )
    resp.raise_for_status()
    buckets = ((resp.json() or {}).get("data") or {}).get("buckets") or []
    if not buckets:
        return 0
    val = (buckets[0].get("computes") or {}).get("c0", 0)
    return int(val) if val is not None else 0


def build_runtime_context(
    owner: str, repo: str, hunks: tuple[DiffHunk, ...],
) -> str | None:
    """The PRODUCTION SIGNAL block for the review prompt, or None when
    Omen has nothing (no mapping, no creds, no hits, or degraded)."""
    service = (get_omen_service_map() or {}).get(f"{owner}/{repo}", "")
    if not service:
        return None  # explicit-allow gate: zero DD calls without a mapping
    try:
        api_key, app_key = get_dd_api_key(), get_dd_app_key()
    except Exception as e:  # noqa: BLE001 — creds unconfigured = feature off
        log.info("omen_degraded", extra={"stage": "creds", "kind": type(e).__name__})
        return None
    if not (api_key and app_key):
        return None

    deadline = time.monotonic() + _TOTAL_BUDGET_SECONDS
    basenames: list[str] = []
    for h in hunks:
        base = h.file_path.rsplit("/", 1)[-1]
        if base and base not in basenames:
            basenames.append(base)

    lines: list[str] = []
    for base in basenames[:_MAX_FILES]:
        try:
            count = _error_count(api_key, app_key, service, base, deadline)
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            log.info(
                "omen_degraded",
                extra={"stage": "query", "file": base, "kind": type(e).__name__},
            )
            continue
        if count is None:
            log.info("omen_degraded", extra={"stage": "budget", "file": base})
            break
        if count < _MIN_ERROR_COUNT:
            continue
        link_query = quote(f'service:{service} status:error "{base}"', safe="")
        lines.append(
            f"- `{base}`: {count} error log(s) in the last 7 days "
            f"([evidence](https://app.{_dd_site()}/logs?query={link_query}&from_ts=now-7d&to_ts=now))"
        )
    if not lines:
        return None
    return (
        f"PRODUCTION SIGNAL (Datadog service `{service}`, last 7 days) — "
        "files in this diff that are ERRORING IN PRODUCTION right now. "
        "Weigh findings on these paths heavier and check the diff guards "
        "the failing path (see hot-path-unguarded rule):\n" + "\n".join(lines)
    )


def _service_map_from_json(raw: str) -> dict[str, str]:
    """Parse the operator's mapping JSON; {} on any malformation (logged
    by the loader). Split out for testability."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(k): str(v) for k, v in parsed.items()
        if isinstance(k, str) and isinstance(v, str) and v
    }
