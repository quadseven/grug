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
infra failure (GitHub 5xx, fetch error) **raises** so the consumer retries via
the visibility timeout and, after `maxReceiveCount`, lands in the DLQ - the
operator-visible "this re-run is stuck" signal. Durable quiet-window reviews
also redrive partial/model/publish failures; explicit operator reruns preserve
the historical published-neutral completion behavior.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import boto3
import httpx

from adapters.install_store import get_repo_config  # type: ignore
from github_app_auth import with_install_token_retry
from github_checks_client import CheckRunResult, post_check_run
from personas.code_reviewer.dispatch import (
    DIFF_TOO_LARGE,
    RETRIED_DEGRADATIONS,
    dispatch_code_review,
)
from personas.code_reviewer.snapshot import (
    review_freshness_id_from_pr,
    review_snapshot_id,
    review_snapshot_id_from_pr,
)
from personas.guard.dispatch import dispatch_guard_review
from personas.smasher.dispatch import dispatch_smasher_review
from personas.walkthrough.dispatch import dispatch_walkthrough_review
from personas.tribe import CHECK_ELDER, acceptable_check_names
from rerun_personas import (
    GUARD as _GUARD,
    RERUNNABLE as _RERUNNABLE,
    SMASHER as _SMASHER,
    TELLER as _TELLER,
)
from rerun_queue import (
    JobNotDue,
    ask_group_id as _ask_group_id,
    learn_group_id as _learn_group_id,
    rerun_group_id as _rerun_group_id,
    review_group_id as _review_group_id,
)

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.rerun")

_GH_API = "https://api.github.com"
_FETCH_TIMEOUT = 15.0

_sqs = boto3.client("sqs")
# Queue URL injected by Pulumi (same env the consumer reads). Unset in
# local/dev/tests -> enqueue raises, surfaced as best-effort by the caller.
_RERUN_QUEUE_URL = os.getenv("GRUG_RERUN_QUEUE_URL", "")
SCHEMA_VERSION = 1
_MAX_SETTLE_SECONDS = 300
# grug#773 AC3: a review discarded and re-enqueued against the SAME head_sha
# this many times running gives up with a terminal neutral check instead of
# looping forever - the backstop for a PR whose title/body keeps changing
# faster than Elder can finish reviewing it. A genuinely new commit always
# resets this to 0; it only counts same-sha redrives.
_MAX_INTENT_DRIFT_REDRIVES = 3
# A learn job whose classifier backends are all rate limited or down (the
# OpenRouter key is free-tier only and refuses intermittently - operator
# decision 2026-09-23) is DEFERRED: re-enqueued as a fresh message with a
# `not_before`, so the wait never counts toward the rerun DLQ's
# maxReceiveCount. Backoff doubles from 15m to a 6h cap (one SQS visibility
# hide covers it; SQS allows 12h). Past 7 days the job completes with the
# in-thread "reply again later" notice, so a reply is never dead-lettered
# and never silently lost.
_LEARN_DEFER_BASE_SECONDS = 900
_LEARN_DEFER_MAX_SECONDS = 6 * 3600
_LEARN_DEFER_MAX_AGE_SECONDS = 7 * 86400


def _now() -> float:
    """Wall clock, as a seam tests can pin."""
    return time.time()


def _learn_defer_delay(defer_count: int) -> int:
    """Seconds to wait before the next classify attempt of a deferred job."""
    return min(
        _LEARN_DEFER_MAX_SECONDS,
        _LEARN_DEFER_BASE_SECONDS * 2 ** min(max(defer_count, 0), 16),
    )
# The lease matches the queue's fallback visibility timeout and is renewed on
# the same cadence as the SQS visibility heartbeat while a review is active.
_REVIEW_CLAIM_LEASE_SECONDS = 900
_REVIEW_CLAIM_HEARTBEAT_SECONDS = 120.0
# How often the mid-flight staleness watcher re-fetches the PR while Elder is
# actually generating (#635 follow-up). Separate from the 120s claim-lease
# heartbeat above, which exists to stop SQS redelivery, not to catch a
# superseding commit quickly - a superseded review should die within roughly
# this many seconds of the new commit landing, not run to its full budget.
_STALENESS_WATCH_INTERVAL_S = 10.0
# Must match personas/code_reviewer/dispatch.py:_CHECK_NAME — this is the
# REQUIRED status-check context on grug-gated repos. Posting it as
# in_progress at enqueue time is what stops GitHub rulesets from treating a
# multi-minute durable review as "required check never ran" (BLOCKED).
# Accept legacy "Grug - Code Review" when listing existing runs mid-cutover.
_ELDER_CHECK_NAME = CHECK_ELDER
_ELDER_CHECK_NAMES = frozenset(acceptable_check_names(CHECK_ELDER))

# Skip reasons where a retry can plausibly succeed (model backend outage,
# unparseable model output, transient diff-fetch error). These raise for SQS
# redrive instead of terminal fail-open completion. Moved-head staleness,
# freshness brownouts, and unknown skips complete neutral; same-head
# staleness and ineligible (draft/closed) exits intentionally stay
# non-terminal - the requeued/reopen review completes the pending check.
# Sourced from the persona rather than restated, so the retry lane and the
# board/email gate cannot drift apart. `worth_an_email` suppresses the board
# for exactly these reasons on the grounds that a retry is coming; if this set
# grew a member here only, that promise would silently become a lie.
_RETRYABLE_SKIP_REASONS = RETRIED_DEGRADATIONS

# Skips that ALREADY published their own terminal check, so the fail-open net
# must leave them alone. Re-posting a generic "Elder skipped - <reason>" over
# one of these replaces real content with strictly less.
#
# `partial_review` belongs here and was missing: it publishes a full check
# naming which cohorts completed, yet fell through to the "unknown skip"
# branch, which overwrote it. That is why one PR showed "Elder skipped -
# partial_review" on the check while the board said "Degraded (partial_review)
# - advisory only" - two surfaces describing one pass in different words,
# because two different code paths wrote them.
#
# DIFF_TOO_LARGE belongs here for the same reason: its degraded check already
# says the diff is over GitHub's size limit and what to do about it, and no
# retry changes GitHub's answer.
_SELF_COMPLETING_SKIP_REASONS = frozenset({
    "no_diff",
    "fail_open_freshness",
    "partial_review",
    DIFF_TOO_LARGE,
})


@dataclass(frozen=True, slots=True)
class _ReviewClaimHeartbeat:
    stop: threading.Event
    ownership_lost: threading.Event
    thread: threading.Thread


@dataclass(frozen=True, slots=True)
class _StalenessWatch:
    stop: threading.Event
    cancel: threading.Event
    thread: threading.Thread


def _review_dedup_id(
    install_id: int, repo: str, pr_number: int, requested_snapshot_id: str,
) -> str:
    """Bounded, full-snapshot FIFO dedup ID."""
    material = (
        f"{install_id}\x1f{repo}\x1f{pr_number}\x1felder\x1f"
        f"{requested_snapshot_id}"
    )
    return f"elder-review:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


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
        MessageGroupId=_rerun_group_id(
            install_id, repo, pr_number, persona,
        ),
        MessageDeduplicationId=f"{install_id}:{repo}:{pr_number}:{persona}",
    )
    log.info(
        "rerun_enqueued",
        extra={"install_id": install_id, "repo": repo, "pr": pr_number, "persona": persona},
    )



def _elder_check_already_terminal_or_pending(
    *,
    install_id: int,
    owner: str,
    repo_name: str,
    head_sha: str,
) -> str | None:
    """Return a skip reason if re-posting in_progress would reopen a settled
    or already-pending Elder check on this head.

    FIFO SQS can accept a send while suppressing delivery for 5 minutes
    (same MessageDeduplicationId). Re-posting in_progress on that path can
    reopen a completed required check with no worker guaranteed to finish
    it. Listing the latest check-run for this name+head is the ground truth.
    """
    def _do(token: str) -> str | None:
        # List without check_name so legacy titles (Grug - Code Review /
        # em-dash variants) still count as Elder during the nomenclature
        # cutover; filter client-side with _ELDER_CHECK_NAMES.
        #
        # Paginate: a busy commit can carry >100 distinct check names even
        # under filter=latest (one run per name), and the Elder run could sit
        # on a later page. Missing it would wrongly re-post in_progress over a
        # settled required check - the exact reopen this guard prevents. Cap
        # the page walk as a runaway backstop (1000 runs >> any real commit).
        _MAX_PAGES = 10
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = (
            f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo_name, safe='')}"
            f"/commits/{quote(head_sha, safe='')}/check-runs"
        )
        runs: list = []
        seen = 0
        for page in range(1, _MAX_PAGES + 1):
            resp = httpx.get(
                url,
                params={"filter": "latest", "per_page": 100, "page": page},
                headers=headers,
                timeout=_FETCH_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json() or {}
            total = int(payload.get("total_count") or 0)
            batch = payload.get("check_runs") or []
            runs.extend(batch)
            seen += len(batch)
            if not batch or seen >= total:
                break
        for run in runs:
            if str(run.get("name") or "") not in _ELDER_CHECK_NAMES:
                continue
            status = str(run.get("status") or "")
            conclusion = str(run.get("conclusion") or "")
            if status in {"queued", "in_progress"}:
                return f"already_{status}"
            if status == "completed":
                # A FAIL-OPEN completion (grug-cr-open external_id) must not
                # suppress a fresh review's pending state: this enqueue IS
                # the worker that will re-complete the check, and leaving the
                # neutral standing keeps the merge button green with a real
                # review in flight (e.g. right after a freshness brownout).
                external = str(run.get("external_id") or "")
                if external.startswith("grug-cr-open:"):
                    return None
                # Any REAL completed conclusion is terminal (including
                # action_required / stale). Creating a NEW in_progress run
                # would reopen the required check with no worker guaranteed.
                return f"already_completed_{conclusion or 'unknown'}"
        return None

    try:
        return with_install_token_retry(install_id, _do)
    except Exception as error:  # noqa: BLE001 - visibility path; prefer post over silent skip on list failure
        log.warning(
            "elder_in_progress_check_list_failed",
            extra={
                "install_id": install_id,
                "repo": f"{owner}/{repo_name}",
                "head_sha": head_sha[:8],
                "kind": type(error).__name__,
            },
        )
        return None



def _complete_elder_check_open(
    *,
    install_id: int,
    owner: str,
    repo_name: str,
    pr_number: int,
    head_sha: str,
    title: str,
    summary: str,
    conclusion: str = "neutral",
) -> bool:
    """Post a TERMINAL Elder check so required-status never sticks in_progress.

    FLINT-style fail-open: infra failure / GH brownout / superseded head
    must complete the required check as neutral (passes merge) with an honest
    title, never leave "in_progress" forever. Never raises. Returns False
    only on a transient post failure (the completion did NOT land, so the
    check may still be stuck); callers on a current-head exit should redrive
    in that case rather than silently finishing the job."""
    if not (owner and repo_name and head_sha):
        # Nothing addressable to complete; retrying cannot help.
        return True
    check = CheckRunResult(
        name=_ELDER_CHECK_NAME,
        head_sha=head_sha,
        status="completed",
        conclusion=conclusion,  # type: ignore[arg-type]
        title=title,
        summary=summary,
    )
    try:
        with_install_token_retry(
            install_id,
            lambda token: post_check_run(
                token,
                owner,
                repo_name,
                check,
                external_id=(
                    f"grug-cr-open:{owner}/{repo_name}"
                    f"#{pr_number}:{head_sha}"
                ),
            ),
        )
        log.info(
            "elder_check_fail_open_completed",
            extra={
                "install_id": install_id,
                "repo": f"{owner}/{repo_name}",
                "pr": pr_number,
                "head_sha": head_sha[:8],
                "conclusion": conclusion,
                "title": title[:80],
            },
        )
        return True
    except Exception as error:  # noqa: BLE001 - visibility path
        log.warning(
            "elder_check_fail_open_failed",
            extra={
                "install_id": install_id,
                "repo": f"{owner}/{repo_name}",
                "pr": pr_number,
                "head_sha": head_sha[:8],
                "kind": type(error).__name__,
            },
        )
        return False


def _post_elder_in_progress_check(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    head_sha: str,
    settle_seconds: int,
) -> None:
    """Best-effort: mark `Grug - Code Review` in_progress for this head.

    Elder is durable + settle-windowed; a deep review routinely takes minutes
    and can be mid-flight-cancelled/re-enqueued when base/title/body moves.
    Without an in_progress check, GitHub required-status rulesets treat the
    PR as BLOCKED ("check missing") for the entire queue+LLM window — the
    failure mode agents keep reading as "Grug Code Review never ran".

    Failures here MUST NOT fail the enqueue: the durable SQS job is the
    correctness path; the pending check is a visibility/UX gate only.
    """
    if "/" not in repo:
        log.warning(
            "elder_in_progress_check_bad_repo",
            extra={"install_id": install_id, "repo": repo, "pr": pr_number},
        )
        return
    owner, repo_name = repo.split("/", 1)
    if not (owner and repo_name and head_sha):
        log.warning(
            "elder_in_progress_check_missing_ids",
            extra={
                "install_id": install_id,
                "repo": repo,
                "pr": pr_number,
                "head_sha": head_sha[:8] if head_sha else "",
            },
        )
        return
    skip_reason = _elder_check_already_terminal_or_pending(
        install_id=install_id,
        owner=owner,
        repo_name=repo_name,
        head_sha=head_sha,
    )
    if skip_reason:
        log.info(
            "elder_in_progress_check_skipped",
            extra={
                "install_id": install_id,
                "repo": repo,
                "pr": pr_number,
                "head_sha": head_sha[:8],
                "reason": skip_reason,
            },
        )
        return
    settle_line = (
        "Swift Hunt: no quiet wait - deep review starts now."
        if settle_seconds <= 0
        else (
            f"Quiet window {settle_seconds}s, then dual-arm deep review "
            "(coder + reasoner on the Cave)."
        )
    )
    check = CheckRunResult(
        name=_ELDER_CHECK_NAME,
        head_sha=head_sha,
        status="in_progress",
        conclusion=None,
        title="Elder is reading the markings",
        summary=(
            f"{settle_line}\n\n"
            "Grug posts this check as soon as the durable review is queued so "
            "required-status rulesets show **pending**, never 'missing'. "
            "Lore (prior findings), Omen (runtime signal), and cross-file "
            "context ride the same pass. Mid-flight cancels re-enqueue when "
            "base/head or real author intent change."
        ),
    )
    try:
        with_install_token_retry(
            install_id,
            lambda token: post_check_run(
                token,
                owner,
                repo_name,
                check,
                external_id=(
                    f"grug-cr-pending:{owner}/{repo_name}"
                    f"#{pr_number}:{head_sha}"
                ),
            ),
        )
    except Exception as error:  # noqa: BLE001 - visibility only; never fail enqueue
        log.warning(
            "elder_in_progress_check_failed",
            extra={
                "install_id": install_id,
                "repo": repo,
                "pr": pr_number,
                "head_sha": head_sha[:8],
                "kind": type(error).__name__,
            },
        )
        return
    log.info(
        "elder_in_progress_check_posted",
        extra={
            "install_id": install_id,
            "repo": repo,
            "pr": pr_number,
            "head_sha": head_sha[:8],
            "settle_seconds": settle_seconds,
        },
    )


def enqueue_review(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    requested_base_sha: str,
    requested_head_sha: str,
    requested_title: str,
    requested_body: str,
    settle_seconds: int,
    redrive_count: int = 0,
) -> None:
    """Enqueue one normal Elder review on the durable consumer lane.

    This differs from an operator/self-recovery rerun: FIFO dedup covers the
    complete review input, not only the head. The consumer still fetches the
    current PR before and after settling, so queued title/body text is never
    trusted as the source of review evidence.

    After a successful SQS send, posts a best-effort in_progress
    `Grug - Code Review` check so required-status rulesets show pending
    rather than "check never ran" while the durable lane works.
    """
    if not _RERUN_QUEUE_URL:
        raise RuntimeError("GRUG_RERUN_QUEUE_URL not configured")
    if not requested_head_sha:
        raise ValueError("requested_head_sha must be non-empty")
    requested_snapshot_id = review_snapshot_id(
        base_sha=requested_base_sha,
        head_sha=requested_head_sha,
        title=requested_title,
        body=requested_body,
    )
    settle = min(_MAX_SETTLE_SECONDS, max(0, int(settle_seconds)))
    _sqs.send_message(
        QueueUrl=_RERUN_QUEUE_URL,
        MessageBody=json.dumps({
            "schema_version": SCHEMA_VERSION,
            "kind": "review",
            "install_id": install_id,
            "repo": repo,
            "pr_number": pr_number,
            "persona": "elder",
            "requested_head_sha": requested_head_sha,
            "requested_snapshot_id": requested_snapshot_id,
            "settle_seconds": settle,
            "redrive_count": redrive_count,
        }),
        MessageGroupId=_review_group_id(install_id, repo, pr_number),
        MessageDeduplicationId=_review_dedup_id(
            install_id, repo, pr_number, requested_snapshot_id,
        ),
    )
    log.info(
        "elder_review_enqueued",
        extra={
            "install_id": install_id,
            "repo": repo,
            "pr": pr_number,
            "head_sha": requested_head_sha[:8],
            "snapshot_id": requested_snapshot_id[:11],
            "settle_seconds": settle,
            "redrive_count": redrive_count,
        },
    )
    # After SQS only: a pre-send check would hang forever if enqueue failed.
    _post_elder_in_progress_check(
        install_id=install_id,
        repo=repo,
        pr_number=pr_number,
        head_sha=requested_head_sha,
        settle_seconds=settle,
    )

# Persona rerun sets + dispatch-routing groups come from the shared
# rerun_personas module (imported at the top) so the API request validator and
# this consumer cannot drift (#581): a rerunnable persona the request rejects
# would be a dead capability - exactly the Teller bug that motivated the split.
# The static TPM check is requestable but logged + skipped below (deliberate).


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
        MessageGroupId=_ask_group_id(install_id, repo, pr_number),
        MessageDeduplicationId=f"{install_id}:{repo}:{pr_number}:ask:{comment_id}",
    )
    log.info("ask_enqueued", extra={"install_id": install_id, "repo": repo,
                                    "pr": pr_number, "comment_id": comment_id})


def enqueue_learn(
    *, install_id: int, repo: str, pr_number: int, comment_id: int,
    parent_comment_id: int, reply_text: str, author: str = "",
    defer_count: int = 0, not_before: float = 0.0, first_deferred_at: float = 0.0,
    requeue_of: str = "",
) -> None:
    """Enqueue a learnings-classification job (#670, ADR-0020) so the LLM
    classifier runs in the consumer, NOT inline in the webhook ACK path.
    `comment_id` is the maintainer's REPLY (the dedup key - a re-delivered
    reply collapses); `parent_comment_id` is grug's finding it answers;
    `author` is the maintainer who taught it (the reply's sender). Runs in
    its OWN FIFO group so a slow classify never serializes with /grug ask.

    `defer_count`/`not_before`/`first_deferred_at` are set only when a job
    is re-enqueued because every classifier backend was rate limited or
    down; the consumer holds it until `not_before`. `requeue_of` is the SQS
    message id of a not-yet-due copy being replaced by a fresh one."""
    if not _RERUN_QUEUE_URL:
        raise RuntimeError("GRUG_RERUN_QUEUE_URL not configured")
    job: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "kind": "learn",
        "install_id": install_id, "repo": repo, "pr_number": pr_number,
        "comment_id": comment_id, "parent_comment_id": parent_comment_id,
        "reply_text": reply_text, "author": author,
    }
    dedup_material = f"{install_id}\x1f{repo}\x1f{pr_number}\x1f{comment_id}"
    if defer_count > 0:
        job.update({
            "defer_count": defer_count, "not_before": not_before,
            "first_deferred_at": first_deferred_at,
        })
        # A distinct dedup id per deferral: the original's id is still inside
        # SQS's 5-minute dedup window when the first deferral is sent, and a
        # collapsed send would lose the job.
        dedup_material += f"\x1fdefer{defer_count}"
    if requeue_of:
        dedup_material += f"\x1frequeue{requeue_of}"
    _sqs.send_message(
        QueueUrl=_RERUN_QUEUE_URL,
        MessageBody=json.dumps(job),
        MessageGroupId=_learn_group_id(install_id, repo, pr_number),
        # Hash the dedup id: a long owner/repo can push the plain string past
        # SQS's 128-char limit and fail send_message (same fix as the group id).
        MessageDeduplicationId="learn:" + hashlib.sha256(
            dedup_material.encode("utf-8")
        ).hexdigest(),
    )
    log.info("learn_enqueued", extra={"install_id": install_id, "repo": repo,
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
    from markdown_safety import neutralize_mentions  # type: ignore
    from observability import emit_gauge  # type: ignore
    owner, _, repo_name = repo_full.partition("/")
    q = _redact_secrets(question)

    def _do(token: str) -> str:
        diff = _gh_get_text(
            token,
            f"{_GH_API}/repos/{_q(owner, safe='')}/{_q(repo_name, safe='')}/pulls/{pr_number}",
            accept="application/vnd.github.v3.diff",
        )
        answer = answer_pr_question(
            q,
            diff,
            install_id,
            pr_context={
                "installation_id": install_id,
                "repo": repo_full,
                "pr_number": pr_number,
            },
        )
        # The answer is model-authored over an attacker-influenceable diff,
        # posted under Grug's own installation-token authority - a live
        # @mention in it would notify a real GitHub user as if Grug itself
        # pinged them (#561, same class as Teller's #554 round-4 fix).
        safe_answer = neutralize_mentions(answer) if answer else answer
        body = (f"{safe_answer}\n\n*(Grug answered from the PR diff - may be wrong; verify.)*"
                if safe_answer else
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


_DEFUSE_MD_CAP = 600


def _defuse_md(s: str) -> str:
    """Neutralize model-produced text before it lands in a GitHub comment
    body. The learning/scope come from the classifier (derived from an
    untrusted reply), so strip HTML-significant chars (a stray </details>
    would break out of the ack's collapsible), backticks/pipes, and flatten
    newlines; cap to bound a hostile payload.

    Logs when the cap actually truncates (PR #679 Elder finding): the raw
    length is captured, not the defused one, since the HTML-escaping above
    can only grow the string - measuring pre-escape avoids under-counting
    a truncation the escaping itself pushed past the cap."""
    defused = (
        s.replace("<", "&lt;").replace(">", "&gt;")
        .replace("`", "'").replace("|", "\\|")
        .replace("\r", " ").replace("\n", " ")
        # A zero-width space after @ breaks GitHub's mention parsing, so a
        # model-restated "@org/team" cannot ping people from grug's ack
        # (renders identically to the reader).
        .replace("@", "@\u200b")
    )
    if len(defused) > _DEFUSE_MD_CAP:
        log.info("defuse_md_truncated", extra={"raw_len": len(s), "cap": _DEFUSE_MD_CAP})
    return defused[:_DEFUSE_MD_CAP]


def _learn_ack_body(learning: str, scope_path: str) -> str:
    """The 'Markings remembered' threaded reply for a stored learning. The
    model-produced fields are markdown-defused so they cannot corrupt the
    comment's <details> rendering or inject unintended mentions."""
    rule = _defuse_md(learning)
    scope = f"\n> Scope: '{_defuse_md(scope_path)}'" if scope_path else ""
    return (
        "Grug remember this.\n\n"
        "<details><summary>Markings remembered</summary>\n\n"
        f"> {rule}{scope}\n\n"
        "Grug will apply this to future reviews on this repo. "
        "So speaks Grug.\n</details>"
    )


_LEARN_DECLINE_BODY = (
    "Grug read this, but hear it as one-time talk for this hunt - "
    "Grug did not carve a lasting marking. Tell Grug a rule for the whole "
    "tribe if you want it remembered."
)

# Every classifier backend is refusing for a config/billing reason. The reply
# was NOT judged; saying so (instead of silence) is the maintainer's cue that a
# re-reply later is how it gets remembered.
_LEARN_UNUSABLE_BODY = (
    "Grug could not think on this right now - the thinking-stones are cold, "
    "and Grug did not judge it. Nothing was remembered. If this is a rule for "
    "the whole tribe, reply again later and Grug will carve it."
)
_LEARN_UNUSABLE: Any = object()  # sentinel: classifier refused, not a verdict
# sentinel: backends stayed rate limited or down past the defer horizon
_LEARN_DEFER_EXHAUSTED: Any = object()


def _post_learn_reply(
    install_id: int, owner: str, repo_name: str, pr_number: int,
    parent_comment_id: int, body: str,
) -> None:
    """Reply in the finding's review thread. Raises on failure; each caller
    decides whether that failure is worth a redrive."""
    from urllib.parse import quote as _q

    def _reply(token: str) -> None:
        _gh_post(
            token,
            f"{_GH_API}/repos/{_q(owner, safe='')}/{_q(repo_name, safe='')}"
            f"/pulls/{pr_number}/comments/{parent_comment_id}/replies",
            {"body": body},
        )
    with_install_token_retry(install_id, _reply)


def _defer_learn(
    install_id: int, repo_full: str, pr_number: int,
    comment_id: int, parent_comment_id: int, reply_text: str, author: str,
    defer_count: int, first_deferred_at: float, statuses: tuple[str, ...],
) -> bool:
    """Re-enqueue a learn job whose classifier backends are all rate limited
    or down. Returns False when the job is past the defer horizon instead.

    Raises if the send fails, so the CURRENT message redrives rather than
    completing with no copy left anywhere."""
    now = _now()
    started = first_deferred_at or now
    if now - started >= _LEARN_DEFER_MAX_AGE_SECONDS:
        log.warning("learn_classifier_defer_exhausted", extra={
            "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
            "defer_count": defer_count, "statuses": list(statuses)})
        return False
    delay = _learn_defer_delay(defer_count)
    enqueue_learn(
        install_id=install_id, repo=repo_full, pr_number=pr_number,
        comment_id=comment_id, parent_comment_id=parent_comment_id,
        reply_text=reply_text, author=author,
        defer_count=defer_count + 1, not_before=now + delay,
        first_deferred_at=started,
    )
    # Warning, not error: on this deployment the free-tier key refusing is
    # expected, and nothing pages on this line.
    log.warning("learn_classifier_deferred", extra={
        "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
        "defer_count": defer_count + 1, "delay_s": delay,
        "statuses": list(statuses)})
    return True


def _run_learn(
    install_id: int, repo_full: str, pr_number: int,
    comment_id: int, parent_comment_id: int, reply_text: str,
    author: str = "", *, defer_count: int = 0, first_deferred_at: float = 0.0,
) -> str:
    """Classify a maintainer's reply to a finding and, if it is a durable team
    preference, store it and acknowledge in the thread (#670, ADR-0020).

    Backend failures split three ways. Every backend rate limited, down or
    unreachable (the free-tier OpenRouter key refuses intermittently) DEFERS
    the job: a delayed copy is enqueued and this message completes, so the
    wait never counts toward the DLQ. Every backend answering 401/402/403/404
    for a config reason completes the job with a notice in the thread, since
    no retry clears it. Answers that do not parse raise for SQS redrive (a
    miss must not be mislabeled a deliberate one-off, and no ack is posted so
    the retry can succeed). A definite verdict (durable OR one-off) is
    win-once per reply comment: the first run acks, a redelivery is a no-op,
    so the finding thread never gets duplicate acknowledgments."""
    from adapters.install_store import (  # type: ignore
        claim_delivery, get_comment_record, get_learning_by_source_comment,
        put_learning,
    )
    from llm_client import (  # type: ignore
        LearnClassifierUnavailable, LearnClassifierUnusable, classify_learning,
    )
    from observability import emit_gauge  # type: ignore

    owner, _, repo_name = repo_full.partition("/")
    record = get_comment_record(install_id, parent_comment_id)
    if record is None:
        # The reply is not to one of grug's tracked findings (or the record
        # TTL-expired). Nothing to learn from; do not post noise.
        log.info("learn_no_parent_record", extra={
            "repo": repo_full, "pr": pr_number, "parent": parent_comment_id})
        return "learn_no_parent"

    # Redelivery fast-path: if THIS reply already produced a stored learning,
    # skip the non-deterministic classifier (a re-run could word the rule
    # differently and store a second row) and reuse the stored rule; the
    # win-once claim below still gates the ack. A transient failure BEFORE
    # the store leaves no row, so the retry classifies cleanly.
    already = get_learning_by_source_comment(repo_full, comment_id)
    if already is not None:
        classification: Any = {
            "durable": True,
            "learning": str(already.get("text", "")),
            "scope_path": str(already.get("scope_path", "")),
        }
    else:
        finding_text = str(record.get("finding_text", ""))
        finding_tags = dict(record.get("finding_tags", {}))
        try:
            classification = classify_learning(
                reply_text, finding_text, finding_tags, install_id,
                pr_context={
                    "installation_id": install_id, "repo": repo_full,
                    "pr_number": pr_number,
                },
            )
        except LearnClassifierUnusable as e:
            # Every backend refused for a config/billing reason (a dead or
            # over-limit key). No redrive clears that, so retrying only walks
            # the job into the rerun DLQ. Complete instead, and tell the
            # maintainer in the thread: re-replying after the fix is the
            # replay path (a new reply is a new job), so the reply is never
            # dropped silently. Operators get llm_backend_unusable plus this
            # line, which names the reply to replay.
            log.error("learn_classifier_unusable", extra={
                "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
                "statuses": list(e.statuses)})
            classification = _LEARN_UNUSABLE
        except LearnClassifierUnavailable as e:
            if _defer_learn(
                install_id, repo_full, pr_number, comment_id,
                parent_comment_id, reply_text, author,
                defer_count, first_deferred_at, e.statuses,
            ):
                return "learn_deferred"
            classification = _LEARN_DEFER_EXHAUSTED
    if classification is None:
        # Transient: backend down or unparseable. Raise for redrive rather
        # than tell the maintainer their durable rule was judged one-off.
        # No store, no claim, no ack, so the retry re-classifies cleanly.
        raise RuntimeError("learn classifier unavailable")

    # STORE FIRST, before the win-once claim. put_learning is idempotent (keyed
    # on the rule digest), so a redelivery re-storing is a harmless no-op - but
    # storing before the claim means a later put/ack failure can never lose the
    # learning (the claim, made only for the ACK, would otherwise short-circuit
    # the retry and drop the rule entirely). FLINT data-integrity fix.
    if classification is _LEARN_UNUSABLE or classification is _LEARN_DEFER_EXHAUSTED:
        # Nothing to store. Unlike the courtesy ack this notice is the
        # maintainer's only cue to re-reply, so it is at-least-once: no
        # win-once claim (a claim taken before a failed post would swallow
        # the retry), and a failed post raises for redrive. A rare SQS
        # duplicate delivery can repeat it; a lost one cannot be recovered.
        _post_learn_reply(
            install_id, owner, repo_name, pr_number, parent_comment_id,
            _LEARN_UNUSABLE_BODY,
        )
        result = (
            "learn_classifier_unusable" if classification is _LEARN_UNUSABLE
            else "learn_defer_exhausted"
        )
        log.info("learn_classified", extra={
            "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
            "result": result})
        return result
    if classification["durable"]:
        put_learning(
            repo=repo_full,
            text=classification["learning"],
            scope_path=classification["scope_path"],
            source_pr=pr_number,
            source_comment_id=comment_id,
            author=author,  # the maintainer who TAUGHT it (reply sender)
        )
        ack = _learn_ack_body(
            classification["learning"], classification["scope_path"],
        )
        result = "learned"
    else:
        # Deliberate one-off: acknowledge without storing.
        ack = _LEARN_DECLINE_BODY
        result = "learn_one_off"

    # The claim guards ONLY the ack (the one non-idempotent side effect): a
    # redelivery whose learning is already stored must not re-post the reply.
    # A genuine RE-TEACH is a NEW reply comment (distinct claim), so it acks.
    if not claim_delivery(f"learn:{comment_id}"):
        log.info("learn_already_acked", extra={
            "repo": repo_full, "pr": pr_number, "comment_id": comment_id})
        return "learn_duplicate"

    # Best-effort: the learning is already durably stored, so a transient reply
    # failure must NOT redrive (which would re-classify + risk a wrong verdict)
    # nor raise - it just costs the courtesy ack, which a re-teach would repost.
    try:
        _post_learn_reply(
            install_id, owner, repo_name, pr_number, parent_comment_id, ack,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("learn_ack_post_failed", extra={
            "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
            "kind": type(e).__name__})
    try:
        emit_gauge("grug.learnings.classified", 1)
    except Exception:  # noqa: BLE001
        pass
    log.info("learn_classified", extra={
        "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
        "result": result})
    return result


def _hold_or_requeue_learn(
    job: dict[str, Any], remaining: float, receive_count: int, message_id: str,
) -> str:
    """A deferred learn job arrived before its `not_before`.

    On its first receive, ask the consumer to hide it until due (JobNotDue).
    A not-due job seen AGAIN means that hide failed, and every early receive
    counts toward the DLQ's maxReceiveCount - left alone, a visibility-API
    outage would dead-letter the reply. So a repeat early arrival is replaced
    by a fresh copy (its own receive count) and this one completes. If that
    send fails too, the raise redrives as usual."""
    if receive_count < 2 or not message_id:
        raise JobNotDue(remaining)
    enqueue_learn(
        install_id=int(job["install_id"]), repo=str(job["repo"]),
        pr_number=int(job["pr_number"]),
        comment_id=int(job.get("comment_id", 0)),
        parent_comment_id=int(job.get("parent_comment_id", 0)),
        reply_text=str(job.get("reply_text", "")),
        author=str(job.get("author", "")),
        defer_count=max(1, int(job.get("defer_count", 0) or 0)),
        not_before=float(job.get("not_before", 0) or 0),
        first_deferred_at=float(job.get("first_deferred_at", 0) or 0),
        requeue_of=message_id,
    )
    log.warning("learn_deferred_requeued", extra={
        "repo": str(job["repo"]), "pr": int(job["pr_number"]),
        "comment_id": int(job.get("comment_id", 0)),
        "receive_count": receive_count, "remaining_s": int(remaining)})
    return "learn_deferred"


def _run_one(body: str, *, receive_count: int = 0, message_id: str = "") -> str:
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
    if job.get("kind") == "learn":
        # A deferred job (every classifier backend was rate limited) arrives
        # at once; FIFO queues have no per-message delay. Hold it until due.
        remaining = float(job.get("not_before", 0) or 0) - _now()
        if remaining > 0:
            return _hold_or_requeue_learn(job, remaining, receive_count, message_id)
        return _run_learn(
            install_id, repo_full, pr_number,
            int(job.get("comment_id", 0)),
            int(job.get("parent_comment_id", 0)),
            str(job.get("reply_text", "")),
            str(job.get("author", "")),
            defer_count=int(job.get("defer_count", 0) or 0),
            first_deferred_at=float(job.get("first_deferred_at", 0) or 0),
        )
    if job.get("kind") == "review":
        return _run_hot_review(job, install_id, repo_full, pr_number)
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

    payload = _review_payload(
        install_id=install_id,
        owner=owner,
        repo_name=repo_name,
        pr_number=pr_number,
        pr=pr,
        action="rerun",
    )
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
    elif persona in _TELLER:
        # Teller has no blocking mode (comment-only, no blocking_flag).
        dispatch_walkthrough_review(payload, blocking=False)
    else:
        dispatch_code_review(
            payload, blocking=bool(cfg.get("code_reviewer_blocking", False)),
        )
    log.info(
        "rerun_dispatched",
        extra={"repo": f"{owner}/{repo_name}", "pr": pr_number, "persona": persona},
    )
    return "dispatched"


def _fetch_current_pr(
    install_id: int, owner: str, repo_name: str, pr_number: int,
) -> dict[str, Any]:
    return with_install_token_retry(
        install_id,
        lambda tok: _gh_get(
            tok,
            f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo_name, safe='')}/pulls/{pr_number}",
        ),
    )


def _review_payload(
    *,
    install_id: int,
    owner: str,
    repo_name: str,
    pr_number: int,
    pr: dict[str, Any],
    action: str = "review",
) -> dict[str, Any]:
    """Current GitHub PR JSON -> the dispatch contract, including intent."""
    base = pr.get("base") or {}
    base_repo = base.get("repo") or {}
    return {
        "action": action,
        "installation": {"id": install_id},
        "repository": {
            "id": int(base_repo["id"]),
            "name": repo_name,
            "owner": {"login": owner},
        },
        "pull_request": {
            "number": pr_number,
            "title": str(pr.get("title") or ""),
            "body": str(pr.get("body") or ""),
            "draft": bool(pr.get("draft", False)),
            "user": {
                "login": str((pr.get("user") or {}).get("login") or ""),
            },
            "head": {"sha": (pr.get("head") or {})["sha"]},
            "base": {"sha": base.get("sha")},
        },
    }


def _review_eligible(pr: dict[str, Any]) -> bool:
    """Only open, non-draft pull requests may publish a review."""
    return str(pr.get("state") or "") == "open" and not bool(
        pr.get("draft", False)
    )


def _enqueue_current_review(
    *,
    install_id: int,
    repo_full: str,
    pr_number: int,
    pr: dict[str, Any],
    settle_seconds: int,
    redrive_count: int = 0,
) -> None:
    """Durably hand the freshly fetched eligible snapshot back to the lane."""
    enqueue_review(
        install_id=install_id,
        repo=repo_full,
        pr_number=pr_number,
        requested_base_sha=str((pr.get("base") or {}).get("sha") or ""),
        requested_head_sha=str((pr.get("head") or {}).get("sha") or ""),
        requested_title=str(pr.get("title") or ""),
        requested_body=str(pr.get("body") or ""),
        settle_seconds=settle_seconds,
        redrive_count=redrive_count,
    )


def _redrive_or_give_up(
    *,
    install_id: int,
    owner: str,
    repo_name: str,
    pr_number: int,
    pr: dict[str, Any],
    settle_seconds: int,
    claimed_head_sha: str,
    current_head_sha: str,
    job_redrive_count: int,
) -> bool:
    """Re-enqueue the freshly fetched snapshot for another pass, or give up
    permanently once a review has been discarded and re-enqueued against the
    SAME head_sha `_MAX_INTENT_DRIFT_REDRIVES` times running (grug#773 AC3) -
    the backstop for a PR whose title/body keeps changing faster than Elder
    can finish reviewing it, which would otherwise repeat this exact
    cancel-then-redrive cycle forever with the check-run stuck `pending`.

    A genuinely NEW commit (current_head_sha != claimed_head_sha) always
    redrives with a fresh count - AC3 is about looping on an UNCHANGED sha,
    never about penalizing an ordinary incremental push. Fail-open (neutral,
    not failure) on giving up, matching the advisory posture every other
    degraded path here already takes.

    Returns True if re-enqueued, False if it gave up instead."""
    repo_full = f"{owner}/{repo_name}"
    same_sha = current_head_sha == claimed_head_sha
    next_redrive_count = job_redrive_count + 1 if same_sha else 0
    if same_sha and next_redrive_count > _MAX_INTENT_DRIFT_REDRIVES:
        log.warning(
            "elder_review_intent_drift_exhausted",
            extra={
                "repo": repo_full,
                "pr": pr_number,
                "head_sha": claimed_head_sha[:8],
                "redrive_count": job_redrive_count,
            },
        )
        _complete_elder_check_open(
            install_id=install_id,
            owner=owner,
            repo_name=repo_name,
            pr_number=pr_number,
            head_sha=claimed_head_sha,
            title="Elder gave up - PR kept changing mid-review",
            summary=(
                f"This PR's title or body changed {job_redrive_count} times "
                "while Elder was reviewing the same commit, faster than a "
                "review could finish. Elder gave up rather than loop "
                "forever (fail-open, merge not blocked). Push a no-op "
                "commit, or wait for edits to settle, then re-request "
                "review."
            ),
            conclusion="neutral",
        )
        return False
    _enqueue_current_review(
        install_id=install_id,
        repo_full=repo_full,
        pr_number=pr_number,
        pr=pr,
        settle_seconds=settle_seconds,
        redrive_count=next_redrive_count,
    )
    return True


def _review_claim_heartbeat_loop(
    owned_claim_args: dict[str, Any],
    stop: threading.Event,
    ownership_lost: threading.Event,
) -> None:
    from adapters.install_store import renew_review_claim

    while not stop.wait(_REVIEW_CLAIM_HEARTBEAT_SECONDS):
        try:
            renewed = renew_review_claim(
                **owned_claim_args,
                lease_seconds=_REVIEW_CLAIM_LEASE_SECONDS,
            )
        except Exception as error:  # noqa: BLE001 - retry within lease buffer
            log.warning(
                "elder_review_claim_heartbeat_failed",
                extra={
                    "repo": owned_claim_args["repo"],
                    "pr": owned_claim_args["pr_number"],
                    "kind": type(error).__name__,
                },
            )
            continue
        if renewed:
            continue
        ownership_lost.set()
        log.error(
            "elder_review_claim_ownership_lost",
            extra={
                "repo": owned_claim_args["repo"],
                "pr": owned_claim_args["pr_number"],
            },
        )
        return


def _review_staleness_watch_loop(
    install_id: int, owner: str, repo_name: str, pr_number: int,
    expected_freshness_id: str,
    expected_head_sha: str,
    stop: threading.Event,
    cancel: threading.Event,
) -> None:
    """Poll the PR every `_STALENESS_WATCH_INTERVAL_S` while Elder is
    actually generating a review (#635 follow-up: mid-flight cancellation).

    Elder's per-arm network calls have no natural checkpoint mid-generation
    - once `_call_backend` starts, nothing inside it re-checks the PR. This
    loop is what lets a superseded review die within roughly
    `_STALENESS_WATCH_INTERVAL_S` of the new commit landing, instead of
    always running to its full ~330-660s budget for a snapshot that's
    already known stale.

    A transient GitHub hiccup must never cancel a genuinely current review,
    so a fetch failure is logged and skipped, not treated as staleness."""
    while not stop.wait(_STALENESS_WATCH_INTERVAL_S):
        try:
            latest = _fetch_current_pr(install_id, owner, repo_name, pr_number)
        except Exception as error:  # noqa: BLE001 - transient fetch failure, keep watching
            log.warning(
                "elder_review_staleness_watch_fetch_failed",
                extra={
                    "repo": f"{owner}/{repo_name}",
                    "pr": pr_number,
                    "kind": type(error).__name__,
                },
            )
            continue
        # HEAD SHA DECIDES. Killing a running review is only justified when
        # the DIFF changed, because that is the only case where the answer
        # being generated is worthless.
        #
        # This used to compare `review_freshness_id`, which also hashes title
        # and body - so editing the PR body aborted both Cave arms mid-
        # generation and threw the whole review away. Live on
        # quadseven/infra#2157 (2026-08-02), a PR breaking a packet-mode
        # bootstrap deadlock: the review started at 13:51:12 over 2 cohorts,
        # the author edited the body at ~13:52:43, and at 13:52:51 it landed
        # as `all_failed` ("cancelled: review input changed while the review
        # was running"). The human got mailed "eyes cloudy - read for self"
        # carrying one cyclomatic-complexity nitpick, on a concurrency fix.
        #
        # An intent edit is exactly the case where the work is still good: the
        # diff is byte-identical, so every finding still anchors to real lines.
        # The pre-publish guard already knows this and publishes such a review
        # (`code_review_intent_drift_publishing_anyway`) - but it had nothing
        # left to publish, because this loop had already killed the generation
        # that would have produced it. Same rule, both halves.
        #
        # Base-insensitive too: `review_snapshot_id_from_pr` hashes base_sha,
        # so ANY unrelated merge to the base branch used to cancel this review.
        # Measured on quadseven/infra 2026-07-25: eight merges in one session
        # left Elder unable to publish, cancelling and re-enqueuing against an
        # identical head. Comparing head sha covers that case as well.
        latest_head = str((latest.get("head") or {}).get("sha") or "")
        if not latest_head:
            # A malformed payload is not evidence of a new commit. Treat it
            # like a fetch failure and keep watching rather than killing a
            # review that is probably fine.
            log.warning(
                "elder_review_staleness_watch_no_head_sha",
                extra={"repo": f"{owner}/{repo_name}", "pr": pr_number},
            )
            continue
        if latest_head != expected_head_sha:
            cancel.set()
            log.info(
                "elder_review_cancelled_mid_flight",
                extra={
                    "repo": f"{owner}/{repo_name}", "pr": pr_number,
                    "reviewed_head_sha": expected_head_sha[:8],
                    "current_head_sha": latest_head[:8],
                    # Carried for diagnosis only. If this DIFFERS while the
                    # shas match, an intent edit happened and was correctly
                    # ignored - that pairing is the signature of the
                    # infra#2157 class, and it should never cancel again.
                    "reviewed_freshness_id": expected_freshness_id[:11],
                    "current_freshness_id": review_freshness_id_from_pr(
                        latest,
                    )[:11],
                },
            )
            return


# --- Active-claim registry (graceful-shutdown release) ----------------------
# A consumer pod that dies mid-review (every deploy rolls it; reviews run
# minutes, the terminationGracePeriod is 30s) orphans its snapshot claim: the
# in-function except/finally never runs, the lease outlives the pod by up to
# _REVIEW_CLAIM_LEASE_SECONDS, and the SQS redelivery bounces off
# "claim busy" - burning receives toward the DLQ (maxReceiveCount) while the
# PR sits without its (now REQUIRED, grug#515) check. The registry tracks
# every in-flight claim so main() can release them all on SIGTERM; the next
# consumer's redelivery then acquires cleanly on its first attempt.
_ACTIVE_REVIEW_CLAIMS: dict[str, dict[str, Any]] = {}
_ACTIVE_REVIEW_CLAIMS_LOCK = threading.Lock()


def _register_active_review_claim(token: str, owned_claim_args: dict[str, Any]) -> None:
    with _ACTIVE_REVIEW_CLAIMS_LOCK:
        _ACTIVE_REVIEW_CLAIMS[token] = owned_claim_args


def _unregister_active_review_claim(token: str) -> None:
    with _ACTIVE_REVIEW_CLAIMS_LOCK:
        _ACTIVE_REVIEW_CLAIMS.pop(token, None)


def release_active_review_claims() -> int:
    """Release every still-registered review claim (graceful shutdown).

    Called by the consumer's main() after its threads were asked to stop: any
    claim still registered belongs to a review that will not finish in this
    process. Releasing lets the SQS redelivery acquire immediately instead of
    bouncing off the orphaned lease. Best-effort per claim - one failed
    release (e.g. ownership already lost to a completing handler racing the
    shutdown) must not stop the rest. Returns the number released."""
    with _ACTIVE_REVIEW_CLAIMS_LOCK:
        claims = list(_ACTIVE_REVIEW_CLAIMS.items())
        _ACTIVE_REVIEW_CLAIMS.clear()
    released = 0
    if not claims:
        return 0
    from adapters.install_store import release_review_claim

    for _token, owned_claim_args in claims:
        try:
            if release_review_claim(**owned_claim_args):
                released += 1
                log.info(
                    "elder_review_claim_released_on_shutdown",
                    extra={
                        "repo": owned_claim_args.get("repo"),
                        "pr": owned_claim_args.get("pr_number"),
                    },
                )
            else:
                log.warning(
                    "elder_review_claim_shutdown_release_lost_ownership",
                    extra={
                        "repo": owned_claim_args.get("repo"),
                        "pr": owned_claim_args.get("pr_number"),
                    },
                )
        except Exception:  # noqa: BLE001 - best-effort during shutdown
            log.warning(
                "elder_review_claim_shutdown_release_failed",
                extra={
                    "repo": owned_claim_args.get("repo"),
                    "pr": owned_claim_args.get("pr_number"),
                },
                exc_info=True,
            )
    return released


def _start_review_claim_heartbeat(
    owned_claim_args: dict[str, Any],
) -> _ReviewClaimHeartbeat:
    stop = threading.Event()
    ownership_lost = threading.Event()
    thread = threading.Thread(
        target=_review_claim_heartbeat_loop,
        args=(owned_claim_args, stop, ownership_lost),
        name="review-claim-heartbeat",
        daemon=True,
    )
    thread.start()
    return _ReviewClaimHeartbeat(
        stop=stop,
        ownership_lost=ownership_lost,
        thread=thread,
    )


def _stop_review_claim_heartbeat(
    heartbeat: _ReviewClaimHeartbeat | None,
) -> bool:
    if heartbeat is None:
        return True
    heartbeat.stop.set()
    heartbeat.thread.join(timeout=1.0)
    return not heartbeat.ownership_lost.is_set()


def _start_staleness_watch(
    install_id: int, owner: str, repo_name: str, pr_number: int,
    expected_freshness_id: str,
    expected_head_sha: str,
) -> _StalenessWatch:
    stop = threading.Event()
    cancel = threading.Event()
    thread = threading.Thread(
        target=_review_staleness_watch_loop,
        args=(
            install_id, owner, repo_name, pr_number,
            expected_freshness_id, expected_head_sha, stop, cancel,
        ),
        name="review-staleness-watch",
        daemon=True,
    )
    thread.start()
    return _StalenessWatch(stop=stop, cancel=cancel, thread=thread)


def _stop_staleness_watch(watch: _StalenessWatch | None) -> None:
    if watch is None:
        return
    watch.stop.set()
    watch.thread.join(timeout=2.0)


def _run_hot_review(
    job: dict[str, Any], install_id: int, repo_full: str, pr_number: int,
) -> str:
    """Settle, cancel stale work, then run Elder on one current snapshot.

    The full-snapshot claim happens before the wait so duplicate webhook events
    skip immediately. Base, head, title, or body movement during the quiet
    window cancels this job; the event for the new snapshot owns the next
    durable message.

    grug#892: `job["requested_head_sha"]` is checked against this SAME live
    fetch before anything else runs (no claim acquired, no settle, no
    dispatch) - a mismatch means a newer push already superseded this job,
    and per FIFO ordering (MessageGroupId is per-PR-per-persona) whatever
    pushed the head also enqueued a fresh job for it, so retiring here is
    safe: the real review happens under that job's own identity and retry
    budget instead of this one's. This is decided ONLY against live PR
    state (`before`, fetched either way) - a consumer cannot reliably
    inspect the queue for a newer message, so the queue is never consulted.
    A stale job that instead failed mid-flight (a GitHub 403, in the
    incident that filed this) used to burn its full SQS retry budget
    before dead-lettering, blocking the real job for the current head
    behind it in the same FIFO group for as long as that took.
    """
    owner, sep, repo_name = repo_full.partition("/")
    if not sep or not owner or not repo_name:
        raise ValueError(f"invalid repo coordinate: {repo_full!r}")

    before = _fetch_current_pr(install_id, owner, repo_name, pr_number)
    head_sha = str((before.get("head") or {}).get("sha") or "")
    if not head_sha:
        raise ValueError("current PR has no head SHA")

    requested_head_sha = str(job.get("requested_head_sha") or "")
    if requested_head_sha and requested_head_sha != head_sha:
        log.info(
            "elder_review_superseded_at_entry",
            extra={
                "repo": repo_full,
                "pr": pr_number,
                "requested_head_sha": requested_head_sha[:8],
                "current_head_sha": head_sha[:8],
            },
        )
        # Best-effort, never raises (see _complete_elder_check_open) - close
        # the SUPERSEDED head's own check so it never sticks in_progress.
        # The current head's check (posted by ITS OWN enqueue) is untouched.
        _complete_elder_check_open(
            install_id=install_id,
            owner=owner,
            repo_name=repo_name,
            pr_number=pr_number,
            head_sha=requested_head_sha,
            title="Elder superseded - new head",
            summary=(
                "A newer commit arrived before this review started. This "
                "head is closed as neutral (fail-open). The review already "
                "queued for the current head handles the real check."
            ),
            conclusion="neutral",
        )
        return "superseded_at_entry"

    if bool(before.get("draft", False)):
        log.info(
            "elder_review_draft_skipped",
            extra={"repo": repo_full, "pr": pr_number, "head_sha": head_sha[:8]},
        )
        return "draft_skipped"
    if str(before.get("state") or "") != "open":
        log.info(
            "elder_review_ineligible_skipped",
            extra={
                "repo": repo_full,
                "pr": pr_number,
                "head_sha": head_sha[:8],
                "state": str(before.get("state") or ""),
            },
        )
        return "pr_ineligible"
    repo_id = int(((before.get("base") or {}).get("repo") or {})["id"])
    snapshot_id = review_snapshot_id_from_pr(before)

    from adapters.install_store import (
        acquire_review_claim,
        complete_review_claim,
        release_review_claim,
    )

    claim_args = {
        "install_id": install_id,
        "repo": repo_full,
        "pr_number": pr_number,
        "persona": "code_reviewer",
        # Legacy store API name; the value is intentionally the canonical
        # review-input identity, while head_sha remains the real commit SHA in
        # dispatch payloads and logs.
        "head_sha": snapshot_id,
    }

    owner_token = uuid.uuid4().hex
    owned_claim_args = {**claim_args, "owner_token": owner_token}
    claim_status = acquire_review_claim(
        **owned_claim_args,
        lease_seconds=_REVIEW_CLAIM_LEASE_SECONDS,
    )
    if claim_status == "completed":
        log.info(
            "elder_review_duplicate_snapshot_skipped",
            extra={
                "repo": repo_full,
                "pr": pr_number,
                "head_sha": head_sha[:8],
                "snapshot_id": snapshot_id[:11],
            },
        )
        return "duplicate_snapshot"
    if claim_status != "acquired":
        log.info(
            "elder_review_snapshot_claim_busy",
            extra={
                "repo": repo_full,
                "pr": pr_number,
                "head_sha": head_sha[:8],
                "snapshot_id": snapshot_id[:11],
            },
        )
        raise RuntimeError("Elder review snapshot claim is still in progress")

    heartbeat: _ReviewClaimHeartbeat | None = None
    # Register for graceful-shutdown release; the finally-unregister runs on
    # every in-process exit (normal, except-release, raise), so the shutdown
    # sweep only ever sees claims whose handler was killed mid-flight.
    _register_active_review_claim(owner_token, owned_claim_args)
    try:
        heartbeat = _start_review_claim_heartbeat(owned_claim_args)
        settle_seconds = min(
            _MAX_SETTLE_SECONDS,
            max(0, int(job.get("settle_seconds", 0))),
        )
        if settle_seconds:
            log.info(
                "elder_review_settling",
                extra={
                    "repo": repo_full,
                    "pr": pr_number,
                    "head_sha": head_sha[:8],
                    "snapshot_id": snapshot_id[:11],
                    "settle_seconds": settle_seconds,
                },
            )
            time.sleep(settle_seconds)

        after = _fetch_current_pr(install_id, owner, repo_name, pr_number)
        current_head = str((after.get("head") or {}).get("sha") or "")
        current_snapshot_id = review_snapshot_id_from_pr(after)
        if not _review_eligible(after):
            if not _stop_review_claim_heartbeat(heartbeat):
                raise RuntimeError(
                    "Elder review claim ownership lost during settle"
                )
            if not release_review_claim(**owned_claim_args):
                raise RuntimeError(
                    "Elder review claim ownership lost for ineligible PR"
                )
            log.info(
                "elder_review_ineligible_after_settle",
                extra={
                    "repo": repo_full,
                    "pr": pr_number,
                    "head_sha": current_head[:8],
                    "state": str(after.get("state") or ""),
                    "draft": bool(after.get("draft", False)),
                },
            )
            return "pr_ineligible"
        if current_snapshot_id != snapshot_id:
            redrived = _redrive_or_give_up(
                install_id=install_id,
                owner=owner,
                repo_name=repo_name,
                pr_number=pr_number,
                pr=after,
                settle_seconds=settle_seconds,
                claimed_head_sha=head_sha,
                current_head_sha=current_head,
                job_redrive_count=int(job.get("redrive_count", 0)),
            )
            if not _stop_review_claim_heartbeat(heartbeat):
                raise RuntimeError(
                    "Elder review claim ownership lost for stale snapshot"
                )
            if not release_review_claim(**owned_claim_args):
                raise RuntimeError(
                    "Elder review claim ownership lost while cancelling stale snapshot"
                )
            log.info(
                "elder_review_stale_snapshot_cancelled",
                extra={
                    "repo": repo_full,
                    "pr": pr_number,
                    "claimed_head_sha": head_sha[:8],
                    "current_head_sha": current_head[:8],
                    "claimed_snapshot_id": snapshot_id[:11],
                    "current_snapshot_id": current_snapshot_id[:11],
                },
            )
            return "stale_snapshot" if redrived else "intent_drift_exhausted"

        cfg = get_repo_config(install_id, repo_id)
        # Mid-flight cancellation (#635 follow-up): current_snapshot_id is the
        # snapshot this job is ABOUT to spend the Elder budget on (checked
        # fresh, immediately above). The watch loop re-fetches the PR every
        # _STALENESS_WATCH_INTERVAL_S for as long as dispatch_code_review is
        # running and sets `cancel` the moment that snapshot changes.
        # dispatch_code_review then returns almost immediately instead of
        # waiting out the arm calls' full remaining duration - the arm
        # calls themselves keep running in the background and get their
        # results discarded (see _call_backend's docstring for why this is
        # "stop waiting on it", not "truly kill it"), but THIS job no
        # longer holds its queue slot / SQS message hostage to them.
        # The watch cancels on HEAD SHA only - the diff is the only input
        # whose change makes the in-flight answer worthless. The freshness id
        # rides along for logging/diagnosis, not as the cancel trigger; it
        # hashes title and body, so using it aborted a running review whenever
        # the author edited their own PR description (infra#2157).
        watch = _start_staleness_watch(
            install_id, owner, repo_name, pr_number,
            review_freshness_id_from_pr(after),
            str((after.get("head") or {}).get("sha") or ""),
        )
        try:
            result = dispatch_code_review(
                _review_payload(
                    install_id=install_id,
                    owner=owner,
                    repo_name=repo_name,
                    pr_number=pr_number,
                    pr=after,
                ),
                blocking=bool(cfg.get("code_reviewer_blocking", False)),
                cancel_event=watch.cancel,
            )
        finally:
            _stop_staleness_watch(watch)
        degraded_reason = result.get("degraded_reason", "")
        if degraded_reason == "stale_snapshot":
            latest = _fetch_current_pr(
                install_id, owner, repo_name, pr_number,
            )
            latest_head = str((latest.get("head") or {}).get("sha") or "")
            if latest_head and latest_head != head_sha:
                # Head actually moved: close the abandoned head's check so it
                # never sticks in_progress. Best-effort - required-status
                # evaluates the NEW head, so a failed post here is cosmetic.
                _complete_elder_check_open(
                    install_id=install_id,
                    owner=owner,
                    repo_name=repo_name,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    title="Elder superseded - new head",
                    summary=(
                        "A newer commit arrived while Elder was reviewing. "
                        "This head is closed as neutral (fail-open). A fresh "
                        "review is enqueued for the current head."
                    ),
                    conclusion="neutral",
                )
            # Same-SHA staleness (title/body intent change): leave the check
            # in_progress - the requeued review on this same head completes
            # it. A terminal neutral here would prematurely green the merge
            # button before the fresh review runs. Unless grug#773 AC3's
            # redrive cap is exhausted, in which case _redrive_or_give_up
            # itself posts the terminal neutral instead of redriving again.
            redrived = (
                _redrive_or_give_up(
                    install_id=install_id,
                    owner=owner,
                    repo_name=repo_name,
                    pr_number=pr_number,
                    pr=latest,
                    settle_seconds=settle_seconds,
                    claimed_head_sha=head_sha,
                    current_head_sha=latest_head,
                    job_redrive_count=int(job.get("redrive_count", 0)),
                )
                if _review_eligible(latest)
                else False
            )
            if not _stop_review_claim_heartbeat(heartbeat):
                raise RuntimeError(
                    "Elder review claim ownership lost during dispatch"
                )
            if not release_review_claim(**owned_claim_args):
                raise RuntimeError(
                    "Elder review claim ownership lost after stale dispatch"
                )
            if not _review_eligible(latest):
                return "pr_ineligible"
            return "stale_snapshot" if redrived else "intent_drift_exhausted"
        if degraded_reason == "pr_ineligible":
            # NO terminal completion here: a draft/closed PR cannot merge, so
            # a lingering in_progress check blocks nothing - but a terminal
            # neutral on this head WOULD satisfy the required check the
            # moment the PR is reopened/marked ready, before the freshly
            # scheduled review posts. Leave the check pending; the
            # ready_for_review/reopen enqueue completes it via a real review.
            if not _stop_review_claim_heartbeat(heartbeat):
                raise RuntimeError(
                    "Elder review claim ownership lost during dispatch"
                )
            if not release_review_claim(**owned_claim_args):
                raise RuntimeError(
                    "Elder review claim ownership lost for ineligible dispatch"
                )
            return "pr_ineligible"
        result_status = result.get("result")
        if result_status == "publish_failed":
            raise RuntimeError("Elder review publication failed")
        # `review_rejected` (#770) is deliberately NOT in that raise: GitHub
        # returned a deterministic 4xx on the inline review (422 for a
        # comment outside the diff). The check-run already landed, so the
        # merge gate is intact; redriving would re-send the identical
        # payload and collect the identical answer five times over - which
        # is exactly how four Elder jobs reached the DLQ. It completes below
        # like pass/fail; dispatch already logged the status and body.
        # Fail-open like FLINT: infra / GH brownout must COMPLETE the
        # required check as neutral, never leave in_progress forever and
        # never redrive forever. Real model findings still use pass/fail.
        if result_status == "skipped" and degraded_reason == "freshness_check_failed":
            posted = _complete_elder_check_open(
                install_id=install_id,
                owner=owner,
                repo_name=repo_name,
                pr_number=pr_number,
                head_sha=head_sha,
                title="Elder eyes clouded - GitHub unavailable",
                summary=(
                    "Could not re-fetch the PR to confirm snapshot freshness "
                    "(GitHub 5xx / transport). Grug fail-open: required check "
                    "concludes **neutral** so merge is not blocked by infra. "
                    "Push again or re-run Elder when GitHub is healthy."
                ),
                conclusion="neutral",
            )
            if not posted:
                # The same brownout ate the neutral completion: finishing the
                # job now would leave the required check in_progress forever
                # with no retry - the exact bug fail-open exists to prevent.
                # Raise for redrive; a later attempt reviews or re-posts.
                raise RuntimeError(
                    "Elder fail-open completion did not land (freshness outage)"
                )
            if not _stop_review_claim_heartbeat(heartbeat):
                raise RuntimeError(
                    "Elder review claim ownership lost after fail-open"
                )
            if not complete_review_claim(**owned_claim_args):
                # Prefer release over hang if complete fails - but if the
                # fallback release ALSO fails, the claim is still held and
                # silently returning would report success while blocking
                # every future attempt until lease expiry. Raise for redrive.
                if not release_review_claim(**owned_claim_args):
                    raise RuntimeError(
                        "Elder fail-open claim settlement failed (freshness)"
                    )
            return "fail_open_freshness"
        if result_status == "skipped" and degraded_reason in _RETRYABLE_SKIP_REASONS:
            # Model/content-side transient (backend outage, unparseable
            # output, diff fetch blip): a retry can succeed, so release the
            # claim and raise for SQS redrive instead of failing open. These
            # paths publish their own completed degraded check (or redrive
            # re-runs the review), so they cannot stick in_progress; the DLQ
            # poison monitor covers a sustained outage.
            raise RuntimeError(
                f"Elder review degraded: {degraded_reason}"
            )
        if (
            result_status == "skipped"
            and degraded_reason not in _SELF_COMPLETING_SKIP_REASONS
        ):
            # Unknown skip: still fail-open rather than infinite redrive.
            posted = _complete_elder_check_open(
                install_id=install_id,
                owner=owner,
                repo_name=repo_name,
                pr_number=pr_number,
                head_sha=head_sha,
                title=f"Elder skipped - {degraded_reason or 'unknown'}",
                summary=(
                    f"Review returned skipped ({degraded_reason or 'unknown'}). "
                    "Grug fail-open: required check concludes **neutral** so "
                    "infra cannot brick the merge. Re-run Elder if needed."
                ),
                conclusion="neutral",
            )
            if not posted:
                # Fail-open only counts if the completion landed; otherwise
                # redrive so the check cannot stay in_progress forever.
                raise RuntimeError(
                    "Elder fail-open completion did not land "
                    f"(skip: {degraded_reason or 'unknown'})"
                )
            if not _stop_review_claim_heartbeat(heartbeat):
                raise RuntimeError(
                    "Elder review claim ownership lost after fail-open skip"
                )
            if not complete_review_claim(**owned_claim_args):
                # Same both-failed guard as the freshness branch above.
                if not release_review_claim(**owned_claim_args):
                    raise RuntimeError(
                        "Elder fail-open claim settlement failed "
                        f"(skip: {degraded_reason or 'unknown'})"
                    )
            return f"fail_open_{degraded_reason or 'skipped'}"
        if (
            result_status not in {"pass", "fail", "skipped", "review_rejected"}
            and not str(result_status).startswith("fail_open")
        ):
            raise RuntimeError(
                f"Elder review returned unexpected result: {result_status!r}"
            )
        if not _stop_review_claim_heartbeat(heartbeat):
            raise RuntimeError("Elder review claim ownership lost during review")
        if not complete_review_claim(**owned_claim_args):
            raise RuntimeError("Elder review claim completion lost ownership")
    except Exception:
        _stop_review_claim_heartbeat(heartbeat)
        try:
            released = release_review_claim(**owned_claim_args)
            if not released:
                raise RuntimeError("Elder review claim release lost ownership")
        except Exception as release_error:  # noqa: BLE001 - preserve primary failure
            log.error(
                "elder_review_claim_release_failed",
                extra={
                    "repo": repo_full,
                    "pr": pr_number,
                    "head_sha": head_sha[:8],
                    "snapshot_id": snapshot_id[:11],
                    "kind": type(release_error).__name__,
                },
                exc_info=True,
            )
        raise
    finally:
        _unregister_active_review_claim(owner_token)
    log.info(
        "elder_review_durable_done",
        extra={"repo": repo_full, "pr": pr_number, **result},
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
        attrs = rec.get("attributes", {}) if isinstance(rec, dict) else {}
        try:
            receive_count = int((attrs or {}).get("ApproximateReceiveCount", 0))
        except (TypeError, ValueError):
            receive_count = 0
        message_id = str(rec.get("messageId", "")) if isinstance(rec, dict) else ""
        # may raise -> ESM retry -> DLQ
        statuses.append(_run_one(
            body, receive_count=receive_count, message_id=message_id,
        ))
    return {
        "records": len(records),
        "dispatched": statuses.count("dispatched"),
        "skipped": sum(
            1 for status in statuses
            if status in {
                "skipped_persona", "duplicate_snapshot", "stale_snapshot",
                "draft_skipped", "pr_ineligible", "superseded_at_entry",
                "intent_drift_exhausted",
            }
        ),
    }
