"""Pulse persona (#472, epic #464 slice 8) - the stuck-PR nudge TRACER,
the first SCHEDULED (non-webhook) persona.

Runs inside the existing grug-poller CronJob cadence (its own pass,
gated independently): for each pulse-enabled repo of an allowlisted
install, find open PRs with no update for `_STALE_DAYS` whose Grug DoR
check is green, and mark them idle (or post a legacy comment when configured)
per `_NUDGE_TTL_DAYS` (idempotent via a store claim). Hard-capped per
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
from adapters.install_store import (
    claim_pulse_nudge,
    get_repo_config,
    release_pulse_nudge,
)
from github_app_auth import get_app_id

from personas import board_client
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
_IDLE_LABEL = "grug:idle"

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
_MAX_LABEL_PAGES = 50


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


def _idle_labeled_prs(token: str, owner: str, repo: str) -> list[dict[str, Any]]:
    """Labeled pull requests, including closed ones, for label cleanup.

    Newest-updated first: a PR that moved or closed sorts ahead of the
    still-idle PRs that keep their labels, so the ones cleanup must act on
    are read first and each removal shifts the next ones forward. Anyone
    with triage rights can apply the label, plain issues included, so the
    scan keeps a hard page cap as the backstop (logged, never silent)."""
    out: list[dict[str, Any]] = []
    for page in range(1, _MAX_LABEL_PAGES + 1):
        resp = httpx.get(
            f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues",
            params={"state": "all", "labels": _IDLE_LABEL, "sort": "updated",
                    "direction": "desc", "per_page": 100, "page": page},
            headers=_headers(token), timeout=_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json() or []
        out.extend(item for item in batch if "pull_request" in item)
        if len(batch) < 100:
            return out
    log.warning(
        "pulse_idle_label_scan_page_cap",
        extra={"repo": f"{owner}/{repo}", "pages": _MAX_LABEL_PAGES,
               "labeled_found": len(out)},
    )
    return out


def _label_url(owner: str, repo: str, pr_number: int) -> str:
    return (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/issues/{pr_number}/labels"
    )


def _remove_idle_label(token: str, owner: str, repo: str, pr_number: int) -> None:
    resp = httpx.delete(
        f"{_label_url(owner, repo, pr_number)}/{quote(_IDLE_LABEL, safe='')}",
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    if resp.status_code != 404:
        resp.raise_for_status()


def _set_idle_label(token: str, owner: str, repo: str, pr_number: int) -> None:
    labels_url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/labels"
    label_resp = httpx.get(
        f"{labels_url}/{quote(_IDLE_LABEL, safe='')}",
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    if label_resp.status_code == 404:
        created = httpx.post(
            labels_url,
            json={
                "name": _IDLE_LABEL,
                "color": "ededed",
                "description": "Open pull request has been idle",
            },
            headers=_headers(token), timeout=_FETCH_TIMEOUT,
        )
        if created.status_code not in (201, 422):
            created.raise_for_status()
    else:
        label_resp.raise_for_status()
    resp = httpx.post(
        _label_url(owner, repo, pr_number), json={"labels": [_IDLE_LABEL]},
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()


def _update_idle_board(token: str, owner: str, repo: str, pr: dict[str, Any]) -> None:
    updated = datetime.fromisoformat(pr["updated_at"].replace("Z", "+00:00"))
    days = max(0, (datetime.now(timezone.utc) - updated).days)
    board_client.upsert_board_section(
        token, owner, repo, int(pr["number"]), key="pulse",
        section=f"Idle since {updated.date().isoformat()}, {days} days",
        app_id=get_app_id(), create_if_absent=False,
    )


def _cleanup_idle_labels(
    token: str, owner: str, repo: str, delivery: str,
    stale_prs: list[dict[str, Any]], labeled: list[dict[str, Any]],
) -> None:
    stale_numbers = {int(pr["number"]) for pr in stale_prs}
    for pr in labeled:
        number = int(pr["number"])
        if delivery != "label" or number not in stale_numbers:
            _remove_idle_label(token, owner, repo, number)


def _process_stale_pr(
    token: str, install_id: int, full: str, owner: str, repo: str,
    delivery: str, pr: dict[str, Any],
) -> bool:
    pr_number = int(pr["number"])
    head_sha = ((pr.get("head") or {}).get("sha")) or ""
    if not head_sha or not _dor_green(token, owner, repo, head_sha):
        return False
    if delivery == "label":
        _set_idle_label(token, owner, repo, pr_number)
        _update_idle_board(token, owner, repo, pr)
    if delivery == "comment" and _recent_nudge_exists(token, owner, repo, pr_number):
        return False
    if not claim_pulse_nudge(install_id, full, pr_number):
        return False
    try:
        if delivery == "comment":
            resp = httpx.post(
                f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
                f"/issues/{pr_number}/comments",
                json={"body": _NUDGE_BODY.format(days=_STALE_DAYS)},
                headers=_headers(token), timeout=_FETCH_TIMEOUT,
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if 400 <= e.response.status_code < 500:
            release_pulse_nudge(install_id, full, pr_number)
        raise
    # Ambiguous transport and 5xx outcomes keep the weekly claim. A label write
    # has already succeeded in label mode, and comment mode verifies markers.
    except httpx.RequestError:
        raise
    log.info("pulse_nudged", extra={"install_id": install_id, "repo": full, "pr": pr_number})
    record_check_verdict(
        install_id=install_id, persona_key="pulse", repo=full,
        pr_number=pr_number, head_sha=head_sha, conclusion="neutral",
        summary=f"Drum nudge - PR quiet {_STALE_DAYS}+ days",
        findings_count=0, blocking=False, degraded_reason=None,
    )
    return True


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
            config = get_repo_config(install_id, int(repo_id))
            if not config.get("pulse_enabled", False):
                continue
            delivery = config.get("pulse_delivery", "label")
            stale_prs = _stale_prs(token, owner, name)
            labeled = _idle_labeled_prs(token, owner, name)
            _cleanup_idle_labels(token, owner, name, delivery, stale_prs, labeled)
            for pr in stale_prs:
                if nudged >= _MAX_NUDGES_PER_INSTALL_RUN:
                    break
                nudged += _process_stale_pr(
                    token, install_id, full, owner, name, delivery, pr,
                )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            # One repo's API failure must not stop the pass.
            log.warning(
                "pulse_repo_failed",
                extra={"install_id": install_id, "repo": full, "kind": type(e).__name__},
            )
    return nudged
