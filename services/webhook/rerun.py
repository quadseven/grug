# WEBHOOK-ONLY (NOT mirrored): the SQS consumer for operator-triggered re-runs
# (#305, ADR-0004). The api service ENQUEUES (services/api/rerun.py); only the
# webhook image carries the persona-dispatch + GitHub-App machinery, so the
# consumer lives here — same split as the cave fallback (cave_fallback.py).
"""Re-run consumer (#305, ADR-0004) — grug's backfill for a dropped/`errored`
review.

`lambda_handler.handler` routes the `grug-rerun-jobs` SQS event-source mapping
here (discriminated from `grug-cave-results` by queue ARN). For each job the
consumer fetches the PR's **current** head + diff and re-runs the named persona
via the unchanged `dispatch_code_review`, which posts the check-run and upserts
the `CheckVerdictRecord` — healing the `errored` row in place if the head is
unchanged, appending a fresh row if the PR moved on.

Failure semantics differ from the cave result handler ON PURPOSE: a transient
infra failure (GitHub 5xx, fetch error) **raises** so the ESM retries via the
visibility timeout and, after `maxReceiveCount`, lands in the DLQ — the
operator-visible "this re-run is stuck" signal. (The *review* failing again —
another LLM outage — is NOT an error: `dispatch_code_review` degrades to a
published neutral/`errored` row and returns normally, so the job is "done".)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from adapters.install_store import get_repo_config  # type: ignore
from github_app_auth import with_install_token_retry
from personas.code_reviewer.dispatch import dispatch_code_review

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.rerun")

_GH_API = "https://api.github.com"
_FETCH_TIMEOUT = 15.0

# Personas the re-run can drive today. The motivating case (the 2026-06 Elder
# outage) is the LLM-driven code reviewer; the static TPM check doesn't error
# from outages, so its re-run is a deliberate follow-up (logged + skipped here).
_CODE_REVIEWER = frozenset({"elder", "code_reviewer"})


def _gh_get(token: str, url: str) -> dict[str, Any]:
    resp = httpx.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _run_one(body: str) -> str:
    """Re-run ONE job. Raises on a malformed message or an infra fetch failure
    (→ ESM retry → DLQ). Returns a short status for the batch summary log.

    Keyed by `repo` ("owner/name") — what the Activity row (the trigger) carries
    — NOT a repo_id; the repo_id (for the RepoConfig lookup) is derived from the
    PR's `base.repo.id` in the same fetch."""
    job = json.loads(body)  # malformed → JSONDecodeError → retry → DLQ
    install_id = int(job["install_id"])
    repo_full = str(job["repo"])  # "owner/name"
    pr_number = int(job["pr_number"])
    persona = str(job.get("persona", "elder"))

    if persona not in _CODE_REVIEWER:
        # Not an infra failure — don't retry/DLQ a persona we don't drive yet.
        log.info(
            "rerun_unsupported_persona",
            extra={"persona": persona, "repo": repo_full, "pr": pr_number},
        )
        return "skipped_persona"

    owner, _, repo_name = repo_full.partition("/")
    # Fetch the PR's CURRENT head (+ the repo id, for RepoConfig). A 5xx/
    # RequestError raises → ESM retry → DLQ. with_install_token_retry refreshes
    # a stale token once.
    pr = with_install_token_retry(
        install_id,
        lambda tok: _gh_get(
            tok, f"{_GH_API}/repos/{owner}/{repo_name}/pulls/{pr_number}"
        ),
    )
    repo_id = int(pr["base"]["repo"]["id"])

    payload = {
        "action": "rerun",  # not a real GH action; dispatch only logs it
        "installation": {"id": install_id},
        "repository": {"id": repo_id, "name": repo_name, "owner": {"login": owner}},
        "pull_request": {"number": pr_number, "head": {"sha": pr["head"]["sha"]}},
    }
    blocking = bool(get_repo_config(install_id, repo_id).get("code_reviewer_blocking", False))
    # dispatch_code_review never raises a wire exception: it fetches the diff,
    # re-runs Elder, dual-publishes, and upserts the verdict (heal-in-place on an
    # unchanged head, append on a moved-on PR). A repeat LLM outage degrades to a
    # published `errored` row — the job still completed.
    dispatch_code_review(payload, blocking=blocking)
    log.info(
        "rerun_dispatched",
        extra={"repo": f"{owner}/{repo_name}", "pr": pr_number, "persona": persona},
    )
    return "dispatched"


def handle_rerun_jobs(event: dict[str, Any]) -> dict[str, int]:
    """Consume `grug-rerun-jobs` SQS records (event-source mapping, batch 1).

    Unlike the cave result handler, a failed job is allowed to RAISE so the ESM
    retries it (visibility timeout) → DLQ after `maxReceiveCount`. With batch
    size 1 each invocation owns exactly one message, so a raise re-drives only
    that job. Returns a summary for the structured log on the success path."""
    records = event.get("Records", []) if isinstance(event, dict) else []
    statuses: list[str] = []
    for rec in records:
        body = rec.get("body", "") if isinstance(rec, dict) else ""
        statuses.append(_run_one(body))  # may raise → ESM retry → DLQ
    return {
        "records": len(records),
        "dispatched": statuses.count("dispatched"),
        "skipped": statuses.count("skipped_persona"),
    }
