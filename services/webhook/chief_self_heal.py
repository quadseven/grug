"""Chief self-heal pass for the poller.

`Grug - Chief` is a REQUIRED check on several repos and is published inline
on the webhook path with no durable retry. When GitHub refuses every
check-run POST for a stretch (2026-10-05, twice), the affected PRs have no
Chief run at all and stay merge-blocked until a human comments
`/grug recheck`. This pass does what that comment does, unattended: for
every open PR whose head SHA has no Chief check-run, it re-runs Chief
through `dispatcher.run_chief_recheck`, the same function the slash command
calls. Chief is never reimplemented here.

Shape reused from the other poller passes: per-install token via
`with_install_token_retry`, per-repo and per-PR best-effort, hard caps, and
never raising into the cron. Repo targeting follows the enforcement pass
(GitHub's installation repo list, with the store's per-repo `tpm_enabled`
opt-out via `is_persona_enabled`), because the store's REPO# rows exist only
for repos with explicit config and Chief defaults to ON.

GitHub call budget per poller run (all bounded):
  - per install: ceil(repos / 100) installation-repository pages
  - per Chief-enabled repo: 1 open-PR list (most recently updated 50)
  - per candidate PR (updated in the lookback window, quiet for 2 minutes):
    1 check-runs list (usually one page)
  - per heal: what `/grug recheck` costs (issue fetches plus 1 check-run POST)
The check-run lists are capped per run (`_MAX_CHECK_LISTS`), re-publish
attempts are capped per run (`_MAX_ATTEMPTS`), and a wall-clock deadline
(`_DEADLINE_S`) stops the pass early, so it cannot lengthen the poller by
more than that deadline.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from adapters.install_store import is_persona_enabled
from github_app_auth import with_install_token_retry
from github_checks_client import list_check_runs_for_ref
from personas.tribe import CHECK_CHIEF, acceptable_check_names

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.chief_self_heal")

_GH_API = "https://api.github.com"
_TIMEOUT = 10
_CHIEF_NAMES = frozenset(acceptable_check_names(CHECK_CHIEF))

_MAX_PRS_PER_REPO = 50
# Re-publish ATTEMPTS per poller run (failed attempts count: a persistent
# GitHub outage must not turn into an unbounded retry storm).
_MAX_ATTEMPTS = int(os.getenv("GRUG_CHIEF_SELF_HEAL_MAX_PUBLISH", "20"))
_MAX_CHECK_LISTS = int(os.getenv("GRUG_CHIEF_SELF_HEAL_MAX_CHECK_LISTS", "150"))
# The webhook path may still be publishing for a PR touched this recently.
_QUIET_SECONDS = 120
# PRs idle longer than this are not scanned (the list is sorted by update
# time, so the scan stops at the first one past it).
_LOOKBACK_HOURS = int(os.getenv("GRUG_CHIEF_SELF_HEAL_LOOKBACK_HOURS", "48"))
_DEADLINE_S = float(os.getenv("GRUG_CHIEF_SELF_HEAL_DEADLINE_S", "90"))

_METRIC = "grug.chief.self_heal"


@dataclass
class _Run:
    """Mutable per-run budget shared across installs and repos."""

    deadline: float
    attempts: int = 0
    check_lists: int = 0
    published: int = 0
    failed: int = 0

    def exhausted(self) -> bool:
        return (
            self.attempts >= _MAX_ATTEMPTS
            or self.check_lists >= _MAX_CHECK_LISTS
            or time.monotonic() >= self.deadline
        )


def _emit(outcome: str) -> None:
    try:
        from observability import emit_count  # type: ignore

        emit_count(_METRIC, 1, tags={"outcome": outcome})
    except Exception as e:  # noqa: BLE001 - telemetry never breaks the cron
        log.debug("chief_self_heal_metric_failed", extra={"kind": type(e).__name__})


def _list_recent_open_prs(token: str, owner: str, repo: str) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls",
        params={
            "state": "open", "sort": "updated", "direction": "desc",
            "per_page": _MAX_PRS_PER_REPO,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return (resp.json() or [])[:_MAX_PRS_PER_REPO]


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _heal_pr(
    run: _Run, token: str, install_id: int, owner: str, repo: str, pr: dict[str, Any],
) -> None:
    """List the head SHA's check-runs; re-run Chief if it has none. Never
    raises: a failure is logged with its exception kind and counted."""
    from dispatcher import run_chief_recheck  # local import: heavy persona graph
    from personas.publish_check import PUBLISH_FAILED

    pr_number = pr.get("number")
    head_sha = (pr.get("head") or {}).get("sha")
    full = f"{owner}/{repo}"
    try:
        run.check_lists += 1
        runs = list_check_runs_for_ref(token, owner, repo, head_sha)
        if any(str(r.get("name") or "") in _CHIEF_NAMES for r in runs):
            return
        run.attempts += 1
        _, result_map = run_chief_recheck(
            installation_id=install_id, owner=owner, repo=repo,
            head_sha=head_sha, pr_number=int(pr_number),
            pr_body=pr.get("body") or "",
        )
        if result_map.get("result") == PUBLISH_FAILED:
            raise RuntimeError("chief publish failed")
    except Exception as e:  # noqa: BLE001 - one PR must not abort the pass
        run.failed += 1
        log.warning(
            "chief_self_heal_failed",
            extra={
                "install_id": install_id, "repo": full, "pr": pr_number,
                "head_sha": str(head_sha)[:8], "kind": type(e).__name__,
            },
        )
        _emit("failed")
        return
    run.published += 1
    log.info(
        "chief_self_heal_published",
        extra={
            "install_id": install_id, "repo": full, "pr": pr_number,
            "head_sha": str(head_sha)[:8],
        },
    )
    _emit("published")


def _heal_repo(run: _Run, token: str, install_id: int, owner: str, repo: str) -> None:
    now = datetime.now(timezone.utc)
    too_old = now - timedelta(hours=_LOOKBACK_HOURS)
    too_new = now - timedelta(seconds=_QUIET_SECONDS)
    for pr in _list_recent_open_prs(token, owner, repo):
        if run.exhausted():
            return
        updated = _parse_ts(pr.get("updated_at"))
        if updated is None or not (pr.get("head") or {}).get("sha") or not pr.get("number"):
            continue
        if updated < too_old:
            return  # sorted newest-first: everything after is older still
        if updated > too_new:
            continue
        _heal_pr(run, token, install_id, owner, repo, pr)


def _heal_install(run: _Run, token: str, install_id: int) -> None:
    from github_rulesets_client import list_installation_repos

    for r in list_installation_repos(token):
        if run.exhausted():
            return
        owner, sep, name = str(r.get("full_name") or "").partition("/")
        if not sep or not name:
            continue
        try:
            if not is_persona_enabled(install_id, int(r["id"]), "tpm"):
                continue
            _heal_repo(run, token, install_id, owner, name)
        except Exception as e:  # noqa: BLE001 - one repo must not starve the rest
            run.failed += 1
            log.warning(
                "chief_self_heal_failed",
                extra={
                    "install_id": install_id, "repo": f"{owner}/{name}",
                    "kind": type(e).__name__,
                },
            )
            _emit("failed")


def self_heal_installs(installs: list[int]) -> tuple[int, int]:
    """Poller entry. Returns (published, failed). Never raises."""
    run = _Run(deadline=time.monotonic() + _DEADLINE_S)
    for install_id in installs:
        if run.exhausted():
            break
        try:
            with_install_token_retry(
                install_id,
                lambda token, iid=install_id: _heal_install(run, token, iid),
            )
        except Exception as e:  # noqa: BLE001 - one install must not abort the cron
            run.failed += 1
            log.warning(
                "chief_self_heal_failed",
                extra={"install_id": install_id, "kind": type(e).__name__},
            )
            _emit("failed")
    return run.published, run.failed
