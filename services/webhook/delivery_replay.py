# WEBHOOK-ONLY (NOT mirrored): runs in the grug-webhook image via the poller
# CronJob and the scripts/replay_deliveries.py CLI. The api service never
# replays deliveries, so per ADR-0001 this is not mirrored (like consumer.py /
# poller_handler.py).
"""Replay GitHub App webhook deliveries missed during an outage (#407).

The DoR/TPM check runs inline on the webhook, so a delivery that arrives while
grug is down is lost: GitHub does not automatically redeliver it, leaving a
required check stuck until a human re-triggers (infra #1254). This recovers
those deliveries via the GitHub App webhook-deliveries API (App-JWT auth):

  - GET  /app/hook/deliveries                       list (cursor-paginated)
  - POST /app/hook/deliveries/{id}/attempts         redeliver one

Idempotency (acceptance #4) without any persistence: GitHub reuses the `guid`
across an event's original delivery and its redeliveries (VERIFIED against the
live App 2026-06-15: redelivering id A produced a new row with redelivery=true
sharing A's guid), and a delivery is SUCCESS when its `status_code` is 200-399.
So we only redeliver guids where NO delivery succeeded - the moment a redelivery
returns 2xx the guid shows success and the next pass skips it. The redelivered
webhook also carries that guid as `X-GitHub-Delivery`, so the consumer's
existing `claim_delivery(delivery_id)` dedupes a second processing.

CAUTION: `post_check_run` POSTs the Checks *create* endpoint and is NOT
idempotent on (name, head_sha) - duplicate posts create duplicate check runs -
so correctness rests on the guid-skip + claim_delivery above, NEVER on
re-posting being a no-op. The bounded window only caps API/pagination cost.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx

from github_app_auth import get_app_jwt

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.delivery_replay")

_GH_API = "https://api.github.com"
_TIMEOUT = 10
# Bound pagination so a huge backlog can't run the poller forever; 20 pages x
# 100/page = 2000 most-recent deliveries, far more than any real outage window.
_MAX_PAGES = 20
_PER_PAGE = 100


@dataclass(frozen=True, slots=True)
class Delivery:
    id: int
    guid: str
    status_code: int
    delivered_at: str  # ISO-8601 'Z'; compare as an instant via _parse_dt
    # (fractional seconds break a naive lexicographic compare - see M3 fix)
    event: str
    redelivery: bool


@dataclass(frozen=True, slots=True)
class ReplayReport:
    scanned: int  # deliveries listed within the window
    failed_guids: int  # distinct events with no successful delivery
    redelivered: int  # successful redeliver POSTs
    errors: int  # redeliver POSTs that raised (best-effort, logged)


def _is_success(status_code: int) -> bool:
    """GitHub marks a delivery successful when the receiver answered 200-399."""
    return 200 <= status_code <= 399


def _parse_dt(iso: str) -> datetime:
    """Parse a GitHub `delivered_at` (e.g. '2026-06-15T23:35:17.676Z') to an
    aware datetime. GitHub emits FRACTIONAL seconds, so a lexicographic string
    compare against a whole-second `since` mis-orders the boundary (verified
    live) - compare as instants instead."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _headers(jwt_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse(d: dict) -> Delivery:
    return Delivery(
        id=int(d["id"]),
        guid=str(d["guid"]),
        status_code=int(d.get("status_code") or 0),
        delivered_at=str(d["delivered_at"]),
        event=str(d.get("event") or ""),
        redelivery=bool(d.get("redelivery", False)),
    )


def _next_cursor(resp) -> str | None:
    """Extract the `cursor` query param from the Link rel=next URL, if any."""
    nxt = getattr(resp, "links", {}).get("next")
    if not nxt:
        return None
    vals = parse_qs(urlparse(nxt["url"]).query).get("cursor")
    return vals[0] if vals else None


def guids_needing_replay(deliveries: list[Delivery]) -> list[int]:
    """PURE idempotency core: group by `guid`; for each event whose deliveries
    NEVER succeeded, return the id of its most-recent attempt to redeliver. An
    event with any 200-399 delivery is skipped - it was delivered, replaying it
    would risk a duplicate check (acceptance #4)."""
    by_guid: dict[str, list[Delivery]] = {}
    for d in deliveries:
        by_guid.setdefault(d.guid, []).append(d)
    out: list[int] = []
    for attempts in by_guid.values():
        if any(_is_success(a.status_code) for a in attempts):
            continue
        # Pick the latest attempt by `id` - GitHub delivery ids are monotonic,
        # so this is robust without parsing the fractional-second timestamps.
        latest = max(attempts, key=lambda a: a.id)
        out.append(latest.id)
    return out


def list_deliveries_since(
    since_iso: str, *, http=httpx, jwt_token: str | None = None
) -> list[Delivery]:
    """List App webhook deliveries newer than `since_iso` (newest first),
    following cursor pagination until the window boundary or `_MAX_PAGES`."""
    jwt_token = jwt_token or get_app_jwt()
    since_dt = _parse_dt(since_iso)
    out: list[Delivery] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, object] = {"per_page": _PER_PAGE}
        if cursor:
            params["cursor"] = cursor
        resp = http.get(
            f"{_GH_API}/app/hook/deliveries",
            headers=_headers(jwt_token),
            params=params,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        reached_boundary = False
        for raw in page:
            try:
                d = _parse(raw)
                before = _parse_dt(d.delivered_at) < since_dt
            except Exception as e:  # noqa: BLE001 - one malformed row must NOT
                # abort the whole window; skip + log it (best-effort contract).
                log.warning(
                    "delivery_replay_parse_skipped",
                    extra={"id": (raw or {}).get("id"), "err": str(e)},
                )
                continue
            if before:
                reached_boundary = True
                break
            out.append(d)
        if reached_boundary:
            break
        cursor = _next_cursor(resp)
        if not cursor:
            break  # no next page -> scanned the whole window cleanly
    else:
        # range() exhausted with a cursor still pending: the window is bigger
        # than the page budget. Say so loudly so a narrower --since (or a
        # raised cap) is used, rather than silently under-recovering.
        log.warning(
            "delivery_replay_window_truncated",
            extra={"max_pages": _MAX_PAGES, "scanned": len(out), "since": since_iso},
        )
    return out


def redeliver(delivery_id: int, *, http=httpx, jwt_token: str | None = None) -> None:
    """Ask GitHub to re-send a single delivery to the webhook URL."""
    jwt_token = jwt_token or get_app_jwt()
    resp = http.post(
        f"{_GH_API}/app/hook/deliveries/{delivery_id}/attempts",
        headers=_headers(jwt_token),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()


def replay_since(since_iso: str, *, http=httpx) -> ReplayReport:
    """List deliveries since `since_iso`, redeliver every event that never
    succeeded. Best-effort: one failed redeliver is logged and counted, never
    aborts the rest. Returns a ReplayReport for the caller to log/expose."""
    jwt_token = get_app_jwt()
    deliveries = list_deliveries_since(since_iso, http=http, jwt_token=jwt_token)
    ids = guids_needing_replay(deliveries)
    redelivered = 0
    errors = 0
    for delivery_id in ids:
        try:
            redeliver(delivery_id, http=http, jwt_token=jwt_token)
            redelivered += 1
        except Exception as e:  # noqa: BLE001 - best-effort; keep replaying
            errors += 1
            log.warning(
                "delivery_replay_attempt_failed",
                extra={"delivery_id": delivery_id, "err": str(e)},
            )
    report = ReplayReport(
        scanned=len(deliveries),
        failed_guids=len(ids),
        redelivered=redelivered,
        errors=errors,
    )
    fields = {
        "since": since_iso,
        "scanned": report.scanned,
        "failed_guids": report.failed_guids,
        "redelivered": report.redelivered,
        "errors": report.errors,
    }
    # Systemic failure: there WERE events to replay but EVERY redeliver failed
    # (broken App-JWT, App suspended) - recovery is dead. Escalate to error so a
    # status:error monitor fires, instead of hiding as a pile of warnings that
    # look like one transient blip (mirrors reaction_poll_all_installs_failed).
    if report.failed_guids and report.redelivered == 0:
        log.error("delivery_replay_all_redelivers_failed", extra=fields)
    else:
        log.info("delivery_replay_done", extra=fields)
    return report
