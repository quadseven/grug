"""Check-run sweeper + reconciler (grug#947, epic #887 / #824).

Fixes both silent failures #887 describes, sharing one per-PR pass since
both need the same input: the head SHA's actual check-runs.

**Reconcile** (a required check that never appeared): GitHub has no
built-in reconciliation for "a webhook fired, grug started, and the
check-run POST never happened" - that class of failure never touches the
App webhook-deliveries log `delivery_replay.py` (#407) already replays
from, because from GitHub's side the delivery to grug's inbound endpoint
succeeded with a 2xx. This compares what check-runs exist against what
SHOULD exist for currently-open PRs, and dispatches the missing ones.

**Sweep** (a check-run stuck `in_progress` forever): GitHub never expires
one, so an orphaned run - Elder's own, mid-review 503 - hangs indefinitely,
reading as "work in progress" to every human and every piece of tooling.
Closes any of grug's OWN check-runs (matched by name, never a third-party
run on the same commit) past a bounded age with `conclusion: failure` and
a body that says plainly that it errored.

Store-driven targeting (`check_run_reconcile_enabled`), same opt-in shape
as dep_watch/pulse/reopen_watch (codex PR #489's rationale: no
`/installation/repositories` paging, so a large install can never starve
an enabled repo behind a discovery-page prefix, and an install with
nothing opted in costs zero GitHub calls per cron tick). One flag covers
both halves - they are one cohesive per-PR pass, not two independent
opt-ins an operator would ever want split.

IDEMPOTENCY (acceptance #3/#4): `post_check_run` is NOT idempotent on
(name, head_sha) - see its own docstring, corrected by this same issue.
This module never relies on that. Before dispatching, it lists the
head SHA's actual check-runs (`list_check_runs_for_ref`, ground truth,
no separate persisted key needed) and passes every persona whose name is
already present as `skip_personas` - the dispatch loop then skips exactly
those personas and runs only the ones genuinely absent. The same
list-immediately-before-act shape `delivery_replay.py` uses for its own
idempotency, not a new pattern. The sweeper's own `patch_check_run` is
genuinely idempotent (updates the same object by id), so re-sweeping an
already-closed run on the next tick is harmless.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from adapters.install_store import is_persona_enabled, list_check_run_reconcile_repos
from github_app_auth import with_install_token_retry
from github_checks_client import CheckRunResult, list_check_runs_for_ref, patch_check_run
from personas import registry as persona_registry

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.check_run_reconciler")

_GH_API = "https://api.github.com"
_TIMEOUT = 10
# Runaway backstop for a repo with an unusually large open-PR count, not a
# real ceiling - matches the pagination-bound convention used throughout
# this codebase (delivery_replay._MAX_PAGES, rerun._MAX_PAGES).
_MAX_PR_PAGES = 10

# grug#887: Elder sat in_progress for 90+ minutes after a mid-review 503.
# 60 minutes gives every real review (including a slow deep-arm pass) room
# to finish before the sweeper would touch it - generous by design, since
# closing a run that is still genuinely working would be the false-positive
# version of this same defect.
_STUCK_IN_PROGRESS_MAX_AGE_MINUTES = int(
    os.getenv("GRUG_CHECK_RUN_STUCK_MAX_AGE_MINUTES", "60")
)


def _list_open_pull_requests(token: str, owner: str, repo: str) -> list[dict[str, Any]]:
    """Open PRs for one repo, paginated. Each element carries at least
    `number`, `head.sha`, `body`."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    out: list[dict[str, Any]] = []
    for page in range(1, _MAX_PR_PAGES + 1):
        resp = httpx.get(
            f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls",
            params={"state": "open", "per_page": 100, "page": page},
            headers=headers,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json() or []
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


def expected_personas(installation_id: int, repo_id: int) -> list[persona_registry.PersonaSpec]:
    """Personas that would post a check-run on an ordinary `pull_request`
    dispatch to this repo right now - the same enablement rule
    `_handle_pull_request` applies, so "expected" here means exactly what
    a healthy webhook would have produced, never a stricter or looser set."""
    return [
        spec
        for spec in persona_registry.REGISTRY
        if "pull_request" in spec.events
        and is_persona_enabled(installation_id, repo_id, spec.key)
    ]


_ALL_CHECK_RUN_NAMES = frozenset(spec.check_run_name for spec in persona_registry.REGISTRY)


def _split_missing_present_stuck(
    runs: list[dict[str, Any]], expected: list[persona_registry.PersonaSpec],
) -> tuple[list[persona_registry.PersonaSpec], frozenset[str], list[dict[str, Any]]]:
    """One pass over an already-fetched check-run listing, three answers:

    - missing: expected personas with no check-run on this head at all
    - present_keys: expected personas that already have SOME check-run
      (skip_personas) - by NAME only, any status. A run this same call
      flags as `stuck` still counts as present: sweeping it to
      `conclusion: failure` is itself a real, visible verdict the author
      can act on, not a silence needing a same-cycle auto-retry. Retrying
      here would risk an unbounded loop if the persona keeps timing out
      on identical input every attempt.
    - stuck: grug's OWN check-runs (any registry name, not just `expected` -
      a persona disabled AFTER it started must still get its stuck run
      swept) sitting `in_progress` past the age bound

    One listing serves both the reconcile and sweep halves - they need the
    same ground truth, so fetching it twice would be a wasted GitHub call
    for no additional correctness."""
    by_name: dict[str, dict[str, Any]] = {}
    for run in runs:
        name = str(run.get("name") or "")
        if name:
            by_name[name] = run

    missing = [spec for spec in expected if spec.check_run_name not in by_name]
    present_keys = frozenset(
        spec.key for spec in expected if spec.check_run_name in by_name
    )

    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=_STUCK_IN_PROGRESS_MAX_AGE_MINUTES
    )
    stuck: list[dict[str, Any]] = []
    for name, run in by_name.items():
        if name not in _ALL_CHECK_RUN_NAMES:
            continue  # not grug's - never touch a third-party check-run
        if str(run.get("status") or "") != "in_progress":
            continue
        started_at = run.get("started_at")
        if not started_at:
            continue
        try:
            started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if started < cutoff:
            stuck.append(run)
    return missing, present_keys, stuck


def _close_stuck_run(token: str, owner: str, repo: str, run: dict[str, Any]) -> None:
    """PATCH one stuck run to a plain, honest failure - never a fabricated
    pass, never silence. `patch_check_run` updates the SAME object by id,
    so re-sweeping an already-closed run on a later tick is a harmless
    no-op re-write, not a duplicate."""
    result = CheckRunResult(
        name=str(run.get("name") or ""),
        head_sha=str(run.get("head_sha") or ""),
        status="completed",
        conclusion="failure",
        title="Grug lost track of this review",
        summary=(
            f"This check sat `in_progress` for over "
            f"{_STUCK_IN_PROGRESS_MAX_AGE_MINUTES} minutes with no "
            "conclusion - almost certainly a GitHub or grug outage mid-"
            "review, not a real finding. The reconciler (grug#947) closed "
            "it rather than leave it hanging forever. Push a new commit "
            "to get a real review."
        ),
    )
    patch_check_run(token, owner, repo, int(run["id"]), result)


def _synthetic_pull_request_payload(
    installation_id: int, owner: str, repo: str, repo_id: int, pr: dict[str, Any],
) -> dict[str, Any]:
    """The minimal `pull_request` webhook payload shape `_handle_pull_request`
    needs. `action: synchronize` is in every persona's default
    `PR_UPDATE_ACTIONS`, matching a real "PR still open, re-check it" event
    rather than claiming a fresh `opened` that never happened."""
    return {
        "action": "synchronize",
        "pull_request": {
            "number": pr.get("number"),
            "body": pr.get("body") or "",
            "head": {"sha": (pr.get("head") or {}).get("sha")},
        },
        "repository": {
            "id": repo_id,
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"login": owner},
        },
        "installation": {"id": installation_id},
    }


def reconcile_repo(
    token: str, installation_id: int, owner: str, repo: str, repo_id: int,
) -> tuple[int, int]:
    """Reconcile + sweep one repo's open PRs. Returns (dispatched, swept):
    PRs that had a missing check-run dispatched, and stuck check-runs
    closed out. Best-effort per PR - one PR's failure must not abort the
    rest of the repo's pass."""
    from dispatcher import dispatch  # local import: heavy persona graph

    dispatched = 0
    swept = 0
    for pr in _list_open_pull_requests(token, owner, repo):
        head_sha = (pr.get("head") or {}).get("sha")
        pr_number = pr.get("number")
        if not head_sha or not pr_number:
            continue
        try:
            runs = list_check_runs_for_ref(token, owner, repo, head_sha)
            expected = expected_personas(installation_id, repo_id)
            missing, present_keys, stuck = _split_missing_present_stuck(runs, expected)

            for run in stuck:
                _close_stuck_run(token, owner, repo, run)
                swept += 1
                log.info(
                    "check_run_swept_stuck",
                    extra={
                        "installation_id": installation_id, "owner": owner, "repo": repo,
                        "pr_number": pr_number, "head_sha": head_sha[:8],
                        "check_run_name": run.get("name"),
                        "started_at": run.get("started_at"),
                    },
                )

            if not missing:
                continue
            payload = _synthetic_pull_request_payload(
                installation_id, owner, repo, repo_id, pr,
            )
            dispatch(
                "pull_request", payload,
                delivery_id=f"reconcile:{owner}/{repo}:{head_sha}",
                skip_personas=present_keys,
            )
            dispatched += 1
            log.info(
                "check_run_reconcile_dispatched",
                extra={
                    "installation_id": installation_id, "owner": owner, "repo": repo,
                    "pr_number": pr_number, "head_sha": head_sha[:8],
                    "missing_personas": [spec.key for spec in missing],
                },
            )
        except Exception as e:  # noqa: BLE001 — one PR must not abort the repo pass
            log.warning(
                "check_run_reconcile_pr_failed",
                extra={
                    "installation_id": installation_id, "owner": owner, "repo": repo,
                    "pr_number": pr_number, "kind": type(e).__name__,
                },
            )
    return dispatched, swept


def reconcile_installs(installs: list[int]) -> tuple[int, int]:
    """Top-level entry for the poller cron. Returns (dispatched_plus_swept,
    installs_failed) - same two-int shape as `_hygiene_watch_pass`, summed
    across every opted-in repo so a caller that only logs one pair of
    numbers still sees real activity, not just a repo count."""
    total = 0
    failed = 0
    for install_id in installs:
        try:
            repos = list_check_run_reconcile_repos(install_id)
            if not repos:
                continue
            results = with_install_token_retry(
                install_id,
                lambda token, iid=install_id, rs=repos: [
                    reconcile_repo(token, iid, r["full_name"].split("/", 1)[0],
                                    r["full_name"].split("/", 1)[1], r["id"])
                    for r in rs
                ],
            ) or []
            total += sum(dispatched + swept for dispatched, swept in results)
        except Exception as e:  # noqa: BLE001 — one install must not abort the cron
            log.warning(
                "check_run_reconcile_install_failed",
                extra={"install_id": install_id, "kind": type(e).__name__},
            )
            failed += 1
    return total, failed
