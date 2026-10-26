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
from personas.tribe import CHECK_CHIEF, acceptable_check_names

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.pulse")

_STALE_DAYS = 7
_NUDGE_TTL_DAYS = 7  # at most one nudge per PR per week (store-claimed)
_MAX_NUDGES_PER_INSTALL_RUN = 3
_MAX_REPOS_PER_INSTALL = 30
_MAX_PRS_PER_REPO = 30
_FETCH_TIMEOUT = 10
_DOR_CHECK_NAME = CHECK_CHIEF
_DOR_CHECK_NAMES = frozenset(acceptable_check_names(CHECK_CHIEF))

IDLE_LABEL = "grug:idle"
_IDLE_LABEL_COLOR = "fbca04"
_IDLE_LABEL_DESC = "Pull request is idle (sleep 7+ sunrises with ready plan)"

# "Grug Drum", not "Grug Pulse": the #798 rename changed the CHECK TITLE and
# stopped there, so the persona's own voice kept the old name in the one place
# a human actually reads it. The marker below is a KEY, not a name - every
# nudge already posted carries it, and renaming it would make this module
# blind to its own prior comments and double-post. Display renames, key stays
# (ADR-0002).
_NUDGE_BODY = (
    "Grug Drum beat for this PR - it sleep {days} sunrises. Plan is ready "
    "(Chief nod long ago) but no grug touch it. Tribe forget? If hunt is "
    "dead, close it - open trails confuse the tribe.\n\n"
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


def _recent_nudge_exists(
    token: str, owner: str, repo: str, pr_number: int,
    *, delivery: str = "label", pr: dict[str, Any] | None = None,
) -> bool:
    """A pulse-nudge marker comment or label inside the TTL window already exists
    (codex PR #489): the write-verification that makes retries safe after
    an AMBIGUOUS failure (timeout after GitHub accepted the write)."""
    if delivery == "comment":
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

    if pr is not None and "labels" in pr:
        return any(
            (label.get("name") if isinstance(label, dict) else str(label)) == IDLE_LABEL
            for label in pr.get("labels", [])
        )
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues/{pr_number}/labels",
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    if resp.status_code == 200:
        return any(
            (label.get("name") if isinstance(label, dict) else str(label)) == IDLE_LABEL
            for label in resp.json() or []
        )
    return False


def _dor_green(token: str, owner: str, repo: str, head_sha: str) -> bool:
    """Only nudge PRs whose plan is READY (Chief's check green) - a
    failing-DoR PR has a different problem than staleness."""
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/commits/{head_sha}/check-runs",
        params={"per_page": 50}, headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    for run in (resp.json() or {}).get("check_runs", []):
        if run.get("name") in _DOR_CHECK_NAMES:
            return run.get("conclusion") == "success"
    return False


def _format_idle_line(updated_at: str) -> str:
    """Format one idle line for the board comment: 'Idle since <date>, <n> days'."""
    try:
        updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        date_str = updated_dt.strftime("%Y-%m-%d")
        days = max(1, (datetime.now(timezone.utc) - updated_dt).days)
    except (ValueError, TypeError):
        date_str = updated_at[:10] if len(updated_at) >= 10 else updated_at
        days = _STALE_DAYS
    return f"Idle since {date_str}, {days} days"


def _ensure_idle_label(token: str, owner: str, repo: str) -> None:
    """Create the grug:idle label in this repo if it is missing."""
    name = quote(IDLE_LABEL, safe="")
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/labels/{name}",
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    if resp.status_code == 200:
        return
    resp = httpx.post(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/labels",
        json={"name": IDLE_LABEL, "color": _IDLE_LABEL_COLOR, "description": _IDLE_LABEL_DESC},
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    if resp.status_code not in (201, 422):
        resp.raise_for_status()


def _add_idle_label(token: str, owner: str, repo: str, pr_number: int) -> None:
    """Attach the grug:idle label to a PR."""
    _ensure_idle_label(token, owner, repo)
    resp = httpx.post(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues/{pr_number}/labels",
        json={"labels": [IDLE_LABEL]},
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()


def _remove_idle_label(token: str, owner: str, repo: str, pr_number: int) -> bool:
    """Remove the grug:idle label from a PR. Tolerates 404 (already gone)."""
    resp = httpx.delete(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues/{pr_number}/labels/{quote(IDLE_LABEL, safe='')}",
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    return resp.status_code in (200, 204, 404)


def _write_board_idle_line(
    token: str, owner: str, repo: str, pr_number: int, updated_at: str,
) -> None:
    """Write the idle line into the existing board comment (an edit, so no email)."""
    try:
        from github_app_auth import get_app_id
        app_id = get_app_id()
    except Exception:
        app_id = None
    from personas import board_client
    board_client.upsert_board_section(
        token, owner, repo, pr_number,
        key="pulse",
        section=_format_idle_line(updated_at),
        create_if_absent=False,
        app_id=app_id,
    )


def _clear_board_idle_line(
    token: str, owner: str, repo: str, pr_number: int,
) -> None:
    """Clear the pulse idle section from the board comment, if present."""
    try:
        from github_app_auth import get_app_id
        app_id = get_app_id()
    except Exception:
        app_id = None
    from personas import board_client
    board_client.clear_board_section(
        token, owner, repo, pr_number,
        key="pulse",
        app_id=app_id,
    )


def _sweep_idle_labels(
    token: str, install_id: int, full: str, owner: str, repo: str,
) -> int:
    """Remove grug:idle label on the next pass after a PR was updated, merged or closed."""
    removed = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_STALE_DAYS)
        resp = httpx.get(
            f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues",
            params={"labels": IDLE_LABEL, "state": "all", "per_page": 100},
            headers=_headers(token), timeout=_FETCH_TIMEOUT,
        )
        if resp.status_code == 404:
            return 0
        resp.raise_for_status()
        for item in resp.json() or []:
            pr_number = item.get("number")
            if not pr_number:
                continue
            state = item.get("state", "open")
            updated_str = item.get("updated_at", "")
            should_remove = False
            if state != "open":
                should_remove = True
            elif updated_str:
                try:
                    updated_dt = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                    if updated_dt >= cutoff:
                        should_remove = True
                except (ValueError, TypeError):
                    pass
            if should_remove:
                _remove_idle_label(token, owner, repo, int(pr_number))
                _clear_board_idle_line(token, owner, repo, int(pr_number))
                release_pulse_nudge(install_id, full, int(pr_number))
                removed += 1
                log.info(
                    "pulse_idle_label_removed",
                    extra={
                        "install_id": install_id,
                        "repo": full,
                        "pr": pr_number,
                        "reason": "closed" if state != "open" else "updated",
                    },
                )
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        log.warning(
            "pulse_sweep_failed",
            extra={"repo": full, "kind": type(exc).__name__},
        )
    return removed


def _deliver_nudge(
    token: str, install_id: int, full: str, owner: str, name: str,
    pr_number: int, head_sha: str, updated_at: str, delivery: str,
) -> None:
    """Deliver one nudge via comment or label according to delivery flag."""
    try:
        if delivery == "comment":
            resp = httpx.post(
                f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues/{pr_number}/comments",
                json={"body": _NUDGE_BODY.format(days=_STALE_DAYS)},
                headers=_headers(token), timeout=_FETCH_TIMEOUT,
            )
            resp.raise_for_status()
        else:
            _add_idle_label(token, owner, name, pr_number)
            _write_board_idle_line(token, owner, name, pr_number, updated_at)
    except httpx.HTTPStatusError as e:
        if 400 <= e.response.status_code < 500:
            release_pulse_nudge(install_id, full, pr_number)
        raise
    except httpx.RequestError:
        raise
    log.info(
        "pulse_nudged",
        extra={"install_id": install_id, "repo": full, "pr": pr_number, "delivery": delivery},
    )
    record_check_verdict(
        install_id=install_id,
        persona_key="pulse",
        repo=full,
        pr_number=pr_number,
        head_sha=head_sha,
        conclusion="neutral",
        summary=f"Drum nudge - PR quiet {_STALE_DAYS}+ days",
        findings_count=0,
        blocking=False,
        degraded_reason=None,
    )


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
            cfg = get_repo_config(install_id, int(repo_id))
            if not cfg.get("pulse_enabled", False):
                continue
            delivery = cfg.get("pulse_delivery", "label")
            _sweep_idle_labels(token, install_id, full, owner, name)
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
                # release). A marker comment or label inside the window means a
                # prior tick's ambiguous failure actually landed - never
                # double-post. Racing runs then serialize on the claim's
                # win-once semantics below.
                if _recent_nudge_exists(token, owner, name, pr_number, delivery=delivery, pr=pr):
                    continue
                if not claim_pulse_nudge(install_id, full, pr_number):
                    continue
                _deliver_nudge(
                    token, install_id, full, owner, name,
                    pr_number, head_sha, pr.get("updated_at", ""), delivery,
                )
                nudged += 1
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            # One repo's API failure must not stop the pass.
            log.warning(
                "pulse_repo_failed",
                extra={"install_id": install_id, "repo": full, "kind": type(e).__name__},
            )
    return nudged
