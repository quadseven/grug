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
a body that says plainly that it errored. For Elder and Chief - the runs a
consumer restart can orphan mid-review - the same pass also re-queues the
persona exactly once (grug#1275; see `reconcile_repo`).

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
    """Personas that would post a check-run on an open PR in this repo
    right now. A persona counts only when it posts a real check-run
    (`posts_pr_check_run`), acts on the `synchronize` action this
    reconciler re-dispatches, and is enabled - the same rules a healthy
    webhook dispatch applies, narrowed to real check posters.

    Comment-only personas (Teller, Sentinel) and closed-only personas
    (Warder, Sentinel) never post a check-run on an open PR, and the
    scheduled Pulse personas post none at all. Expecting any of them
    reports them missing forever and re-dispatches every open PR on
    every poller tick (grug#1431)."""
    return [
        spec
        for spec in persona_registry.REGISTRY
        if "pull_request" in spec.events
        and "synchronize" in spec.actions
        and spec.posts_pr_check_run
        and is_persona_enabled(installation_id, repo_id, spec.key)
    ]


_ALL_CHECK_RUN_NAMES = frozenset(spec.check_run_name for spec in persona_registry.REGISTRY)

# grug#1275: only Elder and Chief stuck runs are re-queued after a sweep -
# they are the review verdicts a restart can orphan mid-flight. Every
# other persona's stuck run is swept to a failure and left alone.
_ELDER_CHECK_RUN_NAME = "Grug - Elder"
_REQUEUE_CHECK_RUN_NAMES = frozenset({_ELDER_CHECK_RUN_NAME, "Grug - Chief"})


def _split_missing_present_stuck(
    runs: list[dict[str, Any]], expected: list[persona_registry.PersonaSpec],
) -> tuple[list[persona_registry.PersonaSpec], frozenset[str], list[dict[str, Any]]]:
    """One pass over an already-fetched check-run listing, three answers:

    - missing: expected personas with no check-run on this head at all
    - present_keys: expected personas that already have SOME check-run
      (skip_personas) - by NAME only, any status. A run this same call
      flags as `stuck` still counts as present HERE; `reconcile_repo`
      separately decides whether a stuck Elder/Chief persona is
      re-queued once after its sweep (grug#1275) - that decision, and
      its once-only argument, live there, not in this split.
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


def _close_stuck_run(
    token: str, owner: str, repo: str, run: dict[str, Any], *, requeued: bool = False,
) -> None:
    """PATCH one stuck run to a plain, honest failure - never a fabricated
    pass, never silence. `patch_check_run` updates the SAME object by id,
    so re-sweeping an already-closed run on a later tick is a harmless
    no-op re-write, not a duplicate. A `requeued` run (grug#1275) says a
    fresh review of this commit is on its way instead of asking for a push."""
    next_step = (
        "Grug re-queued a fresh review of this commit (grug#1275); its "
        "check replaces this one."
        if requeued else "Push a new commit to get a real review."
    )
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
            "review, or a consumer restart, not a real finding. The "
            "reconciler (grug#947) closed it rather than leave it hanging "
            f"forever. {next_step}"
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
    PRs that had a missing (or re-queued) persona run started, and stuck
    check-runs closed out. Best-effort per PR - one PR's failure must not
    abort the rest of the repo's pass."""
    dispatched = 0
    swept = 0
    for pr in _list_open_pull_requests(token, owner, repo):
        head_sha = (pr.get("head") or {}).get("sha")
        pr_number = pr.get("number")
        if not head_sha or not pr_number:
            continue
        try:
            pr_dispatched, pr_swept = _reconcile_pr(
                token, installation_id, owner, repo, repo_id, pr,
            )
            dispatched += pr_dispatched
            swept += pr_swept
        except Exception as e:  # noqa: BLE001 — one PR must not abort the repo pass
            log.warning(
                "check_run_reconcile_pr_failed",
                extra={
                    "installation_id": installation_id, "owner": owner, "repo": repo,
                    "pr_number": pr_number, "kind": type(e).__name__,
                },
            )
    return dispatched, swept


def _reconcile_pr(
    token: str, installation_id: int, owner: str, repo: str, repo_id: int,
    pr: dict[str, Any],
) -> tuple[int, int]:
    """One PR: sweep its stuck runs, re-queue a stuck Elder/Chief once
    (grug#1275), and dispatch any persona with no run at all (grug#947).
    Returns (1 if anything was started else 0, runs swept)."""
    from dispatcher import dispatch  # local import: heavy persona graph

    head_sha = str((pr.get("head") or {}).get("sha"))
    pr_number = int(pr["number"])
    runs = list_check_runs_for_ref(token, owner, repo, head_sha)
    expected = expected_personas(installation_id, repo_id)
    missing, present_keys, stuck = _split_missing_present_stuck(runs, expected)
    requeue = _requeue_specs(expected, stuck)
    # Elder rides the durable rerun lane; it is enqueued BEFORE the sweep so
    # a failed send is known when the swept run's summary is written - that
    # run then honestly asks for a push instead of promising a re-review.
    elder_queued = any(spec.check_run_name == _ELDER_CHECK_RUN_NAME for spec in requeue)
    if elder_queued and not _enqueue_elder_recovery(
        installation_id, owner, repo, pr_number,
        [run for run in stuck if str(run.get("name") or "") == _ELDER_CHECK_RUN_NAME],
    ):
        requeue = [s for s in requeue if s.check_run_name != _ELDER_CHECK_RUN_NAME]
    swept = _sweep_stuck_runs(
        token, installation_id, owner, repo, pr_number, head_sha, stuck,
        {spec.check_run_name for spec in requeue},
    )
    # Everything else (Chief, and every missing persona) goes through one
    # inline dispatch.
    inline_requeue = [s for s in requeue if s.check_run_name != _ELDER_CHECK_RUN_NAME]
    to_run = {spec.key for spec in missing} | {spec.key for spec in inline_requeue}
    if to_run:
        dispatch(
            "pull_request",
            _synthetic_pull_request_payload(installation_id, owner, repo, repo_id, pr),
            delivery_id=_recovery_delivery_id(owner, repo, head_sha, inline_requeue, stuck),
            skip_personas=frozenset(key for key in present_keys if key not in to_run),
        )
    if not (to_run or requeue):
        return 0, swept
    log.info(
        "check_run_reconcile_dispatched",
        extra={
            "installation_id": installation_id, "owner": owner, "repo": repo,
            "pr_number": pr_number, "head_sha": head_sha[:8],
            "missing_personas": [spec.key for spec in missing],
            "requeued_personas": [spec.key for spec in requeue],
        },
    )
    return 1, swept


def _sweep_stuck_runs(
    token: str, installation_id: int, owner: str, repo: str,
    pr_number: int, head_sha: str, stuck: list[dict[str, Any]],
    requeued_names: set[str],
) -> int:
    """Close every stuck run out and log it. Returns how many were swept."""
    for run in stuck:
        requeued = str(run.get("name") or "") in requeued_names
        _close_stuck_run(token, owner, repo, run, requeued=requeued)
        log.info(
            "check_run_swept_stuck",
            extra={
                "installation_id": installation_id, "owner": owner, "repo": repo,
                "pr_number": pr_number, "head_sha": head_sha[:8],
                "check_run_name": run.get("name"),
                "started_at": run.get("started_at"),
                "requeued": requeued,
            },
        )
    return len(stuck)


def _requeue_specs(
    expected: list[persona_registry.PersonaSpec], stuck: list[dict[str, Any]],
) -> list[persona_registry.PersonaSpec]:
    """grug#1275: a stuck Elder/Chief run is not only swept - it is
    re-queued ONCE, in the same pass, with no PR comment posted. Once is
    structural, not a stored flag: sweeping completed the orphaned run, so
    no later pass can flag that run stuck again, and the re-queue is keyed
    by the orphaned run's own id. The re-queued review creates a NEW run
    with a fresh started_at, so it gets a full review deadline before it
    could ever be flagged itself. Other personas' stuck runs stay
    sweep-only (the #947 behaviour)."""
    stuck_names = {str(run.get("name") or "") for run in stuck}
    return [
        spec for spec in expected
        if spec.check_run_name in stuck_names
        and spec.check_run_name in _REQUEUE_CHECK_RUN_NAMES
    ]


def _enqueue_elder_recovery(
    installation_id: int, owner: str, repo: str, pr_number: int,
    elder_runs: list[dict[str, Any]],
) -> bool:
    """Re-queue a stuck Elder on the durable rerun lane (grug#1275).

    Not through `dispatch`: the poller is a CronJob with no durable-Elder
    env, so a dispatched Elder would run on a thread inside a process that
    exits at the end of the pass, and its snapshot claim (the orphaned
    review already took it) would skip it anyway. The rerun lane is the
    path `/grug improve` uses: the grug-consumer deployment runs it,
    against the PR's current head, bypassing the snapshot gate. The SQS
    dedup id is keyed by the orphaned run's id, so two passes racing on
    the same run collapse to one job. Returns False (logged, never raised)
    when the send fails: the run is still swept, as a plain failure."""
    from rerun import enqueue_rerun  # local import: rerun imports this module

    run_ids = ",".join(sorted(str(run.get("id")) for run in elder_runs))
    try:
        enqueue_rerun(
            install_id=installation_id,
            repo=f"{owner}/{repo}",
            pr_number=pr_number,
            persona="elder",
            dedup_key=f"restart-recovery:{run_ids}",
        )
    except Exception as e:  # noqa: BLE001 - the sweep must still happen
        log.error(
            "check_run_requeue_failed",
            extra={
                "installation_id": installation_id, "owner": owner, "repo": repo,
                "pr_number": pr_number, "check_run_ids": run_ids,
                "kind": type(e).__name__,
            },
        )
        return False
    return True


def _recovery_delivery_id(
    owner: str, repo: str, head_sha: str,
    requeue: list[persona_registry.PersonaSpec], stuck: list[dict[str, Any]],
) -> str:
    """Key an inline re-queue (Chief) by the orphaned runs' ids, so it
    dedupes per run; a plain missing-persona dispatch keeps the per-head
    reconcile key."""
    if not requeue:
        return f"reconcile:{owner}/{repo}:{head_sha}"
    names = {spec.check_run_name for spec in requeue}
    run_ids = ",".join(sorted(
        str(run.get("id")) for run in stuck if str(run.get("name") or "") in names
    ))
    return f"restart-recovery:{owner}/{repo}:{run_ids}"


def _emit_stuck_check_run_metric(count: int) -> None:
    """grug#948: a DENSE gauge for the one alert #887 explicitly asked for
    - "a grug check run has been in_progress longer than N minutes" - fed
    by THIS pass's own stuck-run detection (`_split_missing_present_stuck`
    via `reconcile_repo`), never a second implementation of it. Emitted
    every poller tick regardless of `count` (0 included): the poller runs
    on a fixed schedule, so a monitor thresholding this VALUE (`> 0`)
    never goes quiet on a healthy stretch the way a fire-only-on-bad-news
    metric would (ADR-0022's same reasoning). Best-effort; telemetry must
    never affect the reconcile pass itself."""
    try:
        from observability import emit_gauge  # type: ignore
        emit_gauge("grug.check_run.stuck_count", float(count))
    except Exception as e:  # noqa: BLE001 - telemetry never breaks the cron
        log.debug("check_run_stuck_gauge_emit_failed", extra={"kind": type(e).__name__})


def reconcile_installs(installs: list[int]) -> tuple[int, int]:
    """Top-level entry for the poller cron. Returns (dispatched_plus_swept,
    installs_failed) - same two-int shape as `_hygiene_watch_pass`, summed
    across every opted-in repo so a caller that only logs one pair of
    numbers still sees real activity, not just a repo count."""
    total = 0
    failed = 0
    total_swept = 0
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
            total_swept += sum(swept for _, swept in results)
        except Exception as e:  # noqa: BLE001 — one install must not abort the cron
            log.warning(
                "check_run_reconcile_install_failed",
                extra={"install_id": install_id, "kind": type(e).__name__},
            )
            failed += 1
    _emit_stuck_check_run_metric(total_swept)
    return total, failed
