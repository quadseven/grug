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
from personas.code_reviewer.dispatch import dispatch_code_review
from personas.code_reviewer.snapshot import (
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
_RETRYABLE_SKIP_REASONS = frozenset({
    "all_failed",
    "parse_failed",
    "fetch_or_parse_failed",
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

    CodeRabbit-style fail-open: infra failure / GH brownout / superseded head
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
) -> None:
    """Enqueue a learnings-classification job (#670, ADR-0020) so the LLM
    classifier runs in the consumer, NOT inline in the webhook ACK path.
    `comment_id` is the maintainer's REPLY (the dedup key - a re-delivered
    reply collapses); `parent_comment_id` is grug's finding it answers;
    `author` is the maintainer who taught it (the reply's sender). Runs in
    its OWN FIFO group so a slow classify never serializes with /grug ask."""
    if not _RERUN_QUEUE_URL:
        raise RuntimeError("GRUG_RERUN_QUEUE_URL not configured")
    _sqs.send_message(
        QueueUrl=_RERUN_QUEUE_URL,
        MessageBody=json.dumps({
            "schema_version": SCHEMA_VERSION, "kind": "learn",
            "install_id": install_id, "repo": repo, "pr_number": pr_number,
            "comment_id": comment_id, "parent_comment_id": parent_comment_id,
            "reply_text": reply_text, "author": author,
        }),
        MessageGroupId=_learn_group_id(install_id, repo, pr_number),
        MessageDeduplicationId=f"{install_id}:{repo}:{pr_number}:learn:{comment_id}",
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


def _learn_ack_body(learning: str, scope_path: str) -> str:
    """The 'Markings remembered' threaded reply for a stored learning."""
    scope = f"\n> Scope: `{scope_path}`" if scope_path else ""
    return (
        "Grug remember this.\n\n"
        "<details><summary>Markings remembered</summary>\n\n"
        f"> {learning}{scope}\n\n"
        "Grug will apply this to future reviews on this repo. "
        "So speaks Grug.\n</details>"
    )


_LEARN_DECLINE_BODY = (
    "Grug read this, but hear it as one-time talk for this hunt - "
    "Grug did not carve a lasting marking. Tell Grug a rule for the whole "
    "tribe if you want it remembered."
)


def _run_learn(
    install_id: int, repo_full: str, pr_number: int,
    comment_id: int, parent_comment_id: int, reply_text: str,
    author: str = "",
) -> str:
    """Classify a maintainer's reply to a finding and, if it is a durable team
    preference, store it and acknowledge in the thread (#670, ADR-0020).

    A classifier BACKEND failure raises for SQS redrive (a transient outage
    must not be mislabeled a deliberate one-off, and no ack is posted so the
    retry can succeed). A definite verdict (durable OR one-off) is win-once
    per reply comment: the first run acks, a redelivery is a no-op, so the
    finding thread never gets duplicate acknowledgments."""
    from urllib.parse import quote as _q
    from adapters.install_store import (  # type: ignore
        claim_delivery, get_comment_record, put_learning,
    )
    from llm_client import classify_learning  # type: ignore
    from observability import emit_gauge  # type: ignore

    owner, _, repo_name = repo_full.partition("/")
    record = get_comment_record(install_id, parent_comment_id)
    if record is None:
        # The reply is not to one of grug's tracked findings (or the record
        # TTL-expired). Nothing to learn from; do not post noise.
        log.info("learn_no_parent_record", extra={
            "repo": repo_full, "pr": pr_number, "parent": parent_comment_id})
        return "learn_no_parent"

    finding_text = str(record.get("finding_text", ""))
    finding_tags = dict(record.get("finding_tags", {}))
    classification = classify_learning(
        reply_text, finding_text, finding_tags, install_id,
        pr_context={
            "installation_id": install_id, "repo": repo_full,
            "pr_number": pr_number,
        },
    )
    if classification is None:
        # Transient: backend down or unparseable. Raise for redrive rather
        # than tell the maintainer their durable rule was judged one-off.
        # No ack + no claim, so the retry re-classifies cleanly (or DLQs).
        raise RuntimeError("learn classifier unavailable")

    # Win-once per reply comment: a redelivery (or a duplicate GitHub delivery)
    # must not re-classify and re-ack. A genuine RE-TEACH is a NEW reply comment
    # (distinct comment_id -> distinct claim), so it correctly acks again.
    if not claim_delivery(f"learn:{comment_id}"):
        log.info("learn_already_processed", extra={
            "repo": repo_full, "pr": pr_number, "comment_id": comment_id})
        return "learn_duplicate"

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

    def _reply(token: str) -> None:
        _gh_post(
            token,
            f"{_GH_API}/repos/{_q(owner, safe='')}/{_q(repo_name, safe='')}"
            f"/pulls/{pr_number}/comments/{parent_comment_id}/replies",
            {"body": ack},
        )
    with_install_token_retry(install_id, _reply)
    try:
        emit_gauge("grug.learnings.classified", 1)
    except Exception:  # noqa: BLE001
        pass
    log.info("learn_classified", extra={
        "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
        "result": result})
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
    if job.get("kind") == "learn":
        return _run_learn(
            install_id, repo_full, pr_number,
            int(job.get("comment_id", 0)),
            int(job.get("parent_comment_id", 0)),
            str(job.get("reply_text", "")),
            str(job.get("author", "")),
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
    )


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
    expected_snapshot_id: str,
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
        if review_snapshot_id_from_pr(latest) != expected_snapshot_id:
            cancel.set()
            log.info(
                "elder_review_cancelled_mid_flight",
                extra={"repo": f"{owner}/{repo_name}", "pr": pr_number},
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
    expected_snapshot_id: str,
) -> _StalenessWatch:
    stop = threading.Event()
    cancel = threading.Event()
    thread = threading.Thread(
        target=_review_staleness_watch_loop,
        args=(install_id, owner, repo_name, pr_number, expected_snapshot_id, stop, cancel),
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
    """
    owner, sep, repo_name = repo_full.partition("/")
    if not sep or not owner or not repo_name:
        raise ValueError(f"invalid repo coordinate: {repo_full!r}")

    before = _fetch_current_pr(install_id, owner, repo_name, pr_number)
    head_sha = str((before.get("head") or {}).get("sha") or "")
    if not head_sha:
        raise ValueError("current PR has no head SHA")
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
            _enqueue_current_review(
                install_id=install_id,
                repo_full=repo_full,
                pr_number=pr_number,
                pr=after,
                settle_seconds=settle_seconds,
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
            return "stale_snapshot"

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
        watch = _start_staleness_watch(
            install_id, owner, repo_name, pr_number, current_snapshot_id,
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
            # button before the fresh review runs.
            if _review_eligible(latest):
                _enqueue_current_review(
                    install_id=install_id,
                    repo_full=repo_full,
                    pr_number=pr_number,
                    pr=latest,
                    settle_seconds=settle_seconds,
                )
            if not _stop_review_claim_heartbeat(heartbeat):
                raise RuntimeError(
                    "Elder review claim ownership lost during dispatch"
                )
            if not release_review_claim(**owned_claim_args):
                raise RuntimeError(
                    "Elder review claim ownership lost after stale dispatch"
                )
            return (
                "stale_snapshot"
                if _review_eligible(latest)
                else "pr_ineligible"
            )
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
        # Fail-open like CodeRabbit: infra / GH brownout must COMPLETE the
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
        if result_status == "skipped" and degraded_reason not in (
            "no_diff", "fail_open_freshness",
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
        if result_status not in {"pass", "fail", "skipped"} and not str(result_status).startswith("fail_open"):
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
        statuses.append(_run_one(body))  # may raise → ESM retry → DLQ
    return {
        "records": len(records),
        "dispatched": statuses.count("dispatched"),
        "skipped": sum(
            1 for status in statuses
            if status in {
                "skipped_persona", "duplicate_snapshot", "stale_snapshot",
                "draft_skipped", "pr_ineligible",
            }
        ),
    }
