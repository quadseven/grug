# WEBHOOK-ONLY (NOT mirrored): the SQS consumer for operator-triggered re-runs
# (#305, ADR-0004). The api service ENQUEUES (services/api/rerun.py); only the
# webhook image carries the persona-dispatch + GitHub-App machinery, so the
# consumer lives here — same split as the cave fallback (cave_fallback.py).
"""Re-run consumer (#305, ADR-0004) — grug's backfill for a dropped/`errored`
review.

`consumer.py` long-polls `grug-rerun-jobs.fifo` and routes each batch here. For each job the
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
from urllib.parse import quote

import boto3
import httpx

from adapters.install_store import get_repo_config  # type: ignore
from github_app_auth import with_install_token_retry
from personas.code_reviewer.dispatch import dispatch_code_review
from personas.guard.dispatch import dispatch_guard_review
from personas.smasher.dispatch import dispatch_smasher_review

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.rerun")

_GH_API = "https://api.github.com"
_FETCH_TIMEOUT = 15.0

_sqs = boto3.client("sqs")
# Queue URL injected by Pulumi (same env the consumer reads). Unset in
# local/dev/tests -> enqueue raises, surfaced as best-effort by the caller.
_RERUN_QUEUE_URL = os.getenv("GRUG_RERUN_QUEUE_URL", "")
SCHEMA_VERSION = 1


def enqueue_rerun(*, install_id: int, repo: str, pr_number: int, persona: str) -> None:
    """Send a `RerunJob` to `grug-rerun-jobs` (the webhook-side producer used by
    Elder self-recovery, #418). Same job shape + FIFO dedup as the api producer:
    content-dedup on `(install, repo, pr, persona)` over the 5-min window, so a
    self-recover enqueue that races an operator re-run (or a second drop) for the
    same PR collapses to one job. `head_sha` is NOT in the key - a re-run always
    targets the PR's CURRENT head. Raises `RuntimeError` when the queue isn't
    configured (the caller treats enqueue as best-effort)."""
    if not _RERUN_QUEUE_URL:
        raise RuntimeError("GRUG_RERUN_QUEUE_URL not configured")
    _sqs.send_message(
        QueueUrl=_RERUN_QUEUE_URL,
        MessageBody=json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "install_id": install_id,
                "repo": repo,
                "pr_number": pr_number,
                "persona": persona,
            }
        ),
        MessageGroupId=str(install_id),
        MessageDeduplicationId=f"{install_id}:{repo}:{pr_number}:{persona}",
    )
    log.info(
        "rerun_enqueued",
        extra={"install_id": install_id, "repo": repo, "pr": pr_number, "persona": persona},
    )

# Personas the re-run can drive today. The motivating case (the 2026-06 Elder
# outage) is the LLM-driven code reviewer; the static TPM check doesn't error
# from outages, so its re-run is a deliberate follow-up (logged + skipped here).
_CODE_REVIEWER = frozenset({"elder", "code_reviewer"})
_GUARD = frozenset({"guard"})
_SMASHER = frozenset({"smasher"})
_RERUNNABLE = _CODE_REVIEWER | _GUARD | _SMASHER


def enqueue_ask(*, install_id: int, repo: str, pr_number: int, comment_id: int, question: str) -> None:
    """Enqueue a `/grug ask` job (#528) so the heavy LLM Q&A runs in the
    consumer, NOT inline in the webhook ACK path. Dedup keys on comment_id
    (each question is distinct - unlike a persona rerun), so a re-delivered
    comment collapses but two different questions do not."""
    if not _RERUN_QUEUE_URL:
        raise RuntimeError("GRUG_RERUN_QUEUE_URL not configured")
    _sqs.send_message(
        QueueUrl=_RERUN_QUEUE_URL,
        MessageBody=json.dumps({
            "schema_version": SCHEMA_VERSION, "kind": "ask",
            "install_id": install_id, "repo": repo, "pr_number": pr_number,
            "comment_id": comment_id, "question": question,
        }),
        MessageGroupId=str(install_id),
        MessageDeduplicationId=f"{install_id}:{repo}:{pr_number}:ask:{comment_id}",
    )
    log.info("ask_enqueued", extra={"install_id": install_id, "repo": repo,
                                    "pr": pr_number, "comment_id": comment_id})


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


def _gh_get_text(token: str, url: str, *, accept: str) -> str:
    resp = httpx.get(
        url, headers={"Authorization": f"Bearer {token}", "Accept": accept},
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def _gh_post(token: str, url: str, json_body: dict[str, Any]) -> None:
    resp = httpx.post(
        url, json=json_body,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()


def _run_ask(install_id: int, repo_full: str, pr_number: int, question: str) -> str:
    """Answer a /grug ask question in the consumer (async, #528). Fetches the
    diff, runs the JSON-constrained Q&A over the REDACTED question + diff, and
    posts the answer as a reply. Records an activity row. Never raises past
    the job (a bad answer degrades to a fallback reply)."""
    from urllib.parse import quote as _q
    from llm_client import _redact_secrets, answer_pr_question  # type: ignore
    from observability import emit_gauge  # type: ignore
    owner, _, repo_name = repo_full.partition("/")
    q = _redact_secrets(question)

    def _do(token: str) -> str:
        diff = _gh_get_text(
            token,
            f"{_GH_API}/repos/{_q(owner, safe='')}/{_q(repo_name, safe='')}/pulls/{pr_number}",
            accept="application/vnd.github.v3.diff",
        )
        answer = answer_pr_question(q, diff, install_id)
        body = (f"{answer}\n\n*(Grug answered from the PR diff - may be wrong; verify.)*"
                if answer else
                "Grug could not answer that right now (the thinking-rock is tired). Try again.")
        _gh_post(
            token,
            f"{_GH_API}/repos/{_q(owner, safe='')}/{_q(repo_name, safe='')}/issues/{pr_number}/comments",
            {"body": body},
        )
        return "answered" if answer else "ask_no_answer"
    result = with_install_token_retry(install_id, _do)
    try:
        emit_gauge("grug.interactive.ask", 1)
    except Exception:  # noqa: BLE001
        pass
    log.info("ask_answered", extra={"repo": repo_full, "pr": pr_number, "result": result})
    return result


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
    if job.get("kind") == "ask":
        return _run_ask(install_id, repo_full, pr_number, str(job.get("question", "")))
    persona = str(job.get("persona", "elder"))

    if persona not in _RERUNNABLE:
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
            tok, f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo_name, safe='')}/pulls/{pr_number}"
        ),
    )
    repo_id = int(pr["base"]["repo"]["id"])

    payload = {
        "action": "rerun",  # not a real GH action; dispatch only logs it
        "installation": {"id": install_id},
        "repository": {"id": repo_id, "name": repo_name, "owner": {"login": owner}},
        "pull_request": {"number": pr_number, "head": {"sha": pr["head"]["sha"]}},
    }
    cfg = get_repo_config(install_id, repo_id)
    # Neither dispatch raises a wire exception: each fetches the diff, re-runs
    # its persona, publishes, and upserts the verdict (heal-in-place on an
    # unchanged head, append on a moved-on PR). A repeat outage degrades to a
    # published `errored` row — the job still completed.
    if persona in _GUARD:
        dispatch_guard_review(
            payload, blocking=bool(cfg.get("guard_blocking", False)),
        )
    elif persona in _SMASHER:
        # Smasher is advisory-only (no blocking flag); the global master switch
        # is re-checked inside dispatch_smasher_review.
        dispatch_smasher_review(payload, blocking=False)
    else:
        dispatch_code_review(
            payload, blocking=bool(cfg.get("code_reviewer_blocking", False)),
        )
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
