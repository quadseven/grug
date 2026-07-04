"""Pulse persona (#472, epic #464 slice 8) - the stuck-PR nudge TRACER,
the first SCHEDULED (non-webhook) persona.

Runs inside the existing grug-poller CronJob cadence (its own pass,
gated independently): for each pulse-enabled repo of an allowlisted
install, find open PRs with no update for `_STALE_DAYS` whose Grug DoR
check is green, and post ONE caveman-voiced nudge comment per PR per
`_NUDGE_TTL_DAYS` (idempotent via a store claim). Hard-capped per
install per run so a backlog can't blow the CronJob budget.

Default OFF per repo (`pulse_enabled`). Best-effort everywhere: one
repo's failure logs and continues.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from activity_log import record_check_verdict
from adapters.install_store import claim_pulse_nudge, get_repo_config, release_pulse_nudge

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.pulse")

_STALE_DAYS = 7
_NUDGE_TTL_DAYS = 7  # at most one nudge per PR per week (store-claimed)
_MAX_NUDGES_PER_INSTALL_RUN = 3
_MAX_REPOS_PER_INSTALL = 30
_MAX_PRS_PER_REPO = 30
_FETCH_TIMEOUT = 10
_DOR_CHECK_NAME = "Grug — Definition of Ready"

_NUDGE_BODY = (
    "Grug Pulse see this PR sleep {days} sunrises. Plan is ready (Chief "
    "nod long ago) but no grug touch it. Tribe forget? If hunt is dead, "
    "close it - open trails confuse the tribe.\n\n"
    "<!-- grug-pulse-nudge -->"
)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


_MAX_PR_PAGES = 5


def _stale_prs(token: str, owner: str, repo: str) -> list[dict[str, Any]]:
    """ALL open PRs untouched for _STALE_DAYS, oldest-updated first.

    Paginates (codex PR #489 r4: a fixed first-page slice could starve
    eligible PRs behind 30 ineligible stale ones). With `sort=updated
    asc`, staleness is a PREFIX property - the first non-stale PR ends
    the scan, so pagination is naturally bounded by the stale backlog,
    with a hard page cap as the backstop (logged, never silent)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_STALE_DAYS)
    out: list[dict[str, Any]] = []
    for page in range(1, _MAX_PR_PAGES + 1):
        resp = httpx.get(
            f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls",
            params={"state": "open", "sort": "updated", "direction": "asc",
                    "per_page": _MAX_PRS_PER_REPO, "page": page},
            headers=_headers(token), timeout=_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json() or []
        for pr in batch:
            updated = pr.get("updated_at", "")
            try:
                if datetime.fromisoformat(updated.replace("Z", "+00:00")) < cutoff:
                    out.append(pr)
                else:
                    # Ascending by updated: everything after is fresher.
                    return out
            except ValueError:
                continue
        if len(batch) < _MAX_PRS_PER_REPO:
            return out
    log.info(
        "pulse_stale_scan_page_cap",
        extra={"repo": f"{owner}/{repo}", "pages": _MAX_PR_PAGES,
               "stale_found": len(out)},
    )
    return out


def _recent_nudge_exists(token: str, owner: str, repo: str, pr_number: int) -> bool:
    """A pulse-nudge marker comment inside the TTL window already exists
    (codex PR #489): the write-verification that makes retries safe after
    an AMBIGUOUS failure (timeout after GitHub accepted the write)."""
    since = (datetime.now(timezone.utc) - timedelta(days=_NUDGE_TTL_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues/{pr_number}/comments",
        params={"since": since, "per_page": 100},
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    return any("grug-pulse-nudge" in (c.get("body") or "") for c in resp.json() or [])


def _dor_green(token: str, owner: str, repo: str, head_sha: str) -> bool:
    """Only nudge PRs whose plan is READY (Chief's check green) - a
    failing-DoR PR has a different problem than staleness."""
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/commits/{head_sha}/check-runs",
        params={"per_page": 50}, headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    for run in (resp.json() or {}).get("check_runs", []):
        if run.get("name") == _DOR_CHECK_NAME:
            return run.get("conclusion") == "success"
    return False


def run_pulse_for_install(
    token: str, install_id: int, repos: list[dict[str, Any]],
) -> int:
    """One Pulse pass for one install. `repos` = the install's repo list
    (id + full_name), fetched by the caller (the poller already holds a
    token). Returns nudges posted. Never raises past a repo."""
    nudged = 0
    for repo in repos[:_MAX_REPOS_PER_INSTALL]:
        if nudged >= _MAX_NUDGES_PER_INSTALL_RUN:
            break
        repo_id = repo.get("id")
        full = repo.get("full_name", "")
        owner, _, name = full.partition("/")
        if not (repo_id and owner and name):
            continue
        try:
            if not get_repo_config(install_id, int(repo_id)).get("pulse_enabled", False):
                continue
            for pr in _stale_prs(token, owner, name):
                if nudged >= _MAX_NUDGES_PER_INSTALL_RUN:
                    break
                pr_number = int(pr["number"])
                head_sha = ((pr.get("head") or {}).get("sha")) or ""
                if not head_sha or not _dor_green(token, owner, name, head_sha):
                    continue
                # Win-once per (install, repo, pr) per TTL window - a
                # lost claim means a nudge inside the window already
                # happened (or a concurrent poller run won it).
                # Write-verification BEFORE the claim (codex PR #489 r3:
                # a verification-READ failure must not burn the weekly
                # slot - before the claim exists there is nothing to
                # release). A marker comment inside the window means a
                # prior tick's ambiguous failure actually landed - never
                # double-post. Racing runs then serialize on the claim's
                # win-once semantics below.
                if _recent_nudge_exists(token, owner, name, pr_number):
                    continue
                if not claim_pulse_nudge(install_id, full, pr_number):
                    continue
                try:
                    resp = httpx.post(
                        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues/{pr_number}/comments",
                        json={"body": _NUDGE_BODY.format(days=_STALE_DAYS)},
                        headers=_headers(token), timeout=_FETCH_TIMEOUT,
                    )
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    if 400 <= e.response.status_code < 500:
                        # DEFINITE no-write (4xx): release so the next
                        # tick retries - the claim represents a
                        # COMPLETED nudge, never a failed attempt.
                        release_pulse_nudge(install_id, full, pr_number)
                    # 5xx = ambiguous (the write may have landed): keep
                    # the claim; the marker pre-check above makes a
                    # future retry safe either way.
                    raise
                except httpx.RequestError:
                    # Ambiguous transport outcome (timeout after accept
                    # is possible): keep the claim - a missed nudge
                    # beats duplicate spam; the marker pre-check governs
                    # any later retry.
                    raise
                nudged += 1
                log.info(
                    "pulse_nudged",
                    extra={"install_id": install_id, "repo": full, "pr": pr_number},
                )
                # Honest Activity row (ADR-0003): a nudge is a completed
                # advisory action, not a review - neutral, zero findings.
                record_check_verdict(
                    install_id=install_id,
                    persona_key="pulse",
                    repo=full,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    conclusion="neutral",
                    summary=f"Pulse nudge - PR quiet {_STALE_DAYS}+ days",
                    findings_count=0,
                    blocking=False,
                    degraded_reason=None,
                )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            # One repo's API failure must not stop the pass.
            log.warning(
                "pulse_repo_failed",
                extra={"install_id": install_id, "repo": full, "kind": type(e).__name__},
            )
    return nudged
