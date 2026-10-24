"""Elder durable-attempt lifecycle: identity, eligibility, claim lease,
freshness, staleness, terminal transitions, redrive policy.

Owns the complete Elder snapshot lifecycle that was previously scattered
across async_dispatch.py, rerun.py, and dispatch.py. Generic SQS and
Postgres behavior stays behind adapter seams (boto3 / psycopg never appear
here); claim ops delegate to the install_store adapter, and SQS / GitHub
side effects are injected as callables so the terminal transitions are
unit-testable without mocking SQS or psycopg at depth.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from github_app_auth import with_install_token_retry

from adapters.install_store import (
    acquire_review_claim as _adapter_acquire_review_claim,
    complete_review_claim as _adapter_complete_review_claim,
    release_review_claim as _adapter_release_review_claim,
    renew_review_claim as _adapter_renew_review_claim,
)

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.elder_review")

# ---------------------------------------------------------------------------
# Identity (canonical snapshot + freshness identity for one Elder input)
# ---------------------------------------------------------------------------

# v2: intent text is normalized (HTML comments + whitespace noise stripped)
# so bot footers and auto-generated body rewrites no longer thrash mid-flight
# Elder reviews while human intent is unchanged.
_SNAPSHOT_VERSION = "elder-review-v2"

# HTML comments are almost always bot/tool footers (release notes blocks,
# metadata markers). They rewrite without changing the author's intent and
# used to force a brand-new snapshot_id -> mid-flight cancel storm.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MULTI_BLANK = re.compile(r"\n{3,}")


def normalize_intent_text(text: str) -> str:
    """Return intent text suitable for snapshot identity.

    Strips HTML comments and collapses blank-line noise. Real human prose
    (title and body) still participates in the hash after cleanup so an
    author rewriting the Why still re-triggers review.
    """
    cleaned = _HTML_COMMENT.sub("", text or "")
    cleaned = _MULTI_BLANK.sub("\n\n", cleaned)
    return cleaned.strip()


def review_snapshot_id(
    *,
    base_sha: str,
    head_sha: str,
    title: str,
    body: str,
) -> str:
    """Return an unambiguous, bounded identity for every reviewed input.

    Head SHA alone is insufficient: changing the base changes the diff, while
    changing title/body changes the intent supplied to the reviewer. JSON
    array encoding preserves field boundaries and exact text without relying
    on delimiter escaping. Intent fields are normalized first so ephemeral
    bot footers do not thrash the durable review lane.
    """
    material = json.dumps(
        [
            _SNAPSHOT_VERSION,
            base_sha,
            head_sha,
            normalize_intent_text(title),
            normalize_intent_text(body),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"v1:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def review_snapshot_id_from_pr(pr: Mapping[str, Any]) -> str:
    """Build the canonical identity from GitHub pull-request JSON."""
    return review_snapshot_id(
        base_sha=str((pr.get("base") or {}).get("sha") or ""),
        head_sha=str((pr.get("head") or {}).get("sha") or ""),
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
    )


def review_freshness_id(
    *,
    head_sha: str,
    title: str,
    body: str,
) -> str:
    """Identity for "is an in-flight review still worth publishing?".

    Deliberately EXCLUDES base_sha, which is the one input the PR author does
    not control and which changes constantly on a busy repo.

    `base_sha` is `pr.base.sha`, the base branch TIP. It moves every time ANY
    other PR merges to the base branch. Because `review_snapshot_id` hashes it,
    a single merge to main changed the snapshot_id of EVERY open PR, which made
    every in-flight Elder review look stale, which cancelled it, which
    re-enqueued it against the same head - and on a repo with a busy afternoon
    that loop never converges.

    What still invalidates a review, and should:
      * head_sha  - the author pushed new code
      * title     - stated intent changed
      * body      - stated intent changed

    What no longer does:
      * base_sha  - somebody else merged something unrelated
    """
    material = json.dumps(
        [
            _SNAPSHOT_VERSION,
            head_sha,
            normalize_intent_text(title),
            normalize_intent_text(body),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"v1:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def review_freshness_id_from_pr(pr: Mapping[str, Any]) -> str:
    """Build the base-insensitive freshness identity from GitHub PR JSON."""
    return review_freshness_id(
        head_sha=str((pr.get("head") or {}).get("sha") or ""),
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
    )


def adaptive_elder_settle_seconds(
    pr: Mapping[str, Any],
    *,
    base_seconds: int,
) -> int:
    """Scale the quiet window to the size of the hunt (Swift Elder).

    Tiny PRs almost never get force-pushed mid-settle; waiting the full
    quiet window only adds empty latency. Large multi-file PRs keep the
    full base window so rapid push storms do not burn dual Cave arms on
    every intermediate head.

    When GitHub omits size stats (additions/deletions/changed_files all
    zero/absent), keep ``base_seconds`` — never invent a "tiny" path from
    missing data.
    """
    base = max(0, int(base_seconds))
    try:
        additions = int(pr.get("additions") or 0)
        deletions = int(pr.get("deletions") or 0)
        changed = int(pr.get("changed_files") or 0)
    except (TypeError, ValueError):
        return base
    churn = max(0, additions) + max(0, deletions)
    changed = max(0, changed)
    if changed == 0 and churn == 0:
        return base
    # Swift Hunt: small, focused PR — start review immediately.
    if changed <= 5 and churn <= 120:
        return 0
    # Steady Hunt: medium PR — short settle only.
    if changed <= 12 and churn <= 400:
        return min(base, 3)
    # Full Hunt: large / noisy PR — full quiet window.
    return base


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def review_eligible(pr: Mapping[str, Any]) -> bool:
    """Only open, non-draft pull requests may publish a review."""
    return str(pr.get("state") or "") == "open" and not bool(
        pr.get("draft", False)
    )


# ---------------------------------------------------------------------------
# Settle (quiet window resolution)
# ---------------------------------------------------------------------------

_DEFAULT_ELDER_SETTLE_SECONDS = 90
_MAX_ELDER_SETTLE_SECONDS = 300


def durable_elder_settle_seconds(pr: dict[str, Any]) -> int:
    """Resolve base settle env + Swift Hunt adaptive cap for one PR."""
    raw_settle = os.getenv(
        "GRUG_ELDER_SETTLE_SECONDS",
        str(_DEFAULT_ELDER_SETTLE_SECONDS),
    )
    try:
        settle_seconds = int(raw_settle)
    except ValueError:
        log.warning(
            "elder_settle_seconds_invalid",
            extra={"value": raw_settle},
        )
        settle_seconds = _DEFAULT_ELDER_SETTLE_SECONDS
    settle_seconds = min(
        _MAX_ELDER_SETTLE_SECONDS, max(0, settle_seconds),
    )
    return adaptive_elder_settle_seconds(pr, base_seconds=settle_seconds)


# ---------------------------------------------------------------------------
# Claim lease (acquire / heartbeat / release / complete)
# ---------------------------------------------------------------------------

# The lease matches the queue's fallback visibility timeout and is renewed on
# the same cadence as the SQS visibility heartbeat while a review is active.
_REVIEW_CLAIM_LEASE_SECONDS = 900
_REVIEW_CLAIM_HEARTBEAT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class ReviewClaimHeartbeat:
    stop: threading.Event
    ownership_lost: threading.Event
    thread: threading.Thread


def acquire_review_claim(
    *, lease_seconds: int = _REVIEW_CLAIM_LEASE_SECONDS, **claim_args: Any,
) -> str:
    """Acquire ownership of one durable Elder attempt."""
    return _adapter_acquire_review_claim(
        **claim_args, lease_seconds=lease_seconds,
    )


def release_review_claim(**claim_args: Any) -> bool:
    """Release an attempt only when its ownership token still matches."""
    return _adapter_release_review_claim(**claim_args)


def complete_review_claim(**claim_args: Any) -> bool:
    """Mark an owned attempt complete through the persistence adapter."""
    return _adapter_complete_review_claim(**claim_args)


def _review_claim_heartbeat_loop(
    owned_claim_args: dict[str, Any],
    stop: threading.Event,
    ownership_lost: threading.Event,
) -> None:
    while not stop.wait(_REVIEW_CLAIM_HEARTBEAT_SECONDS):
        try:
            renewed = _adapter_renew_review_claim(
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


def start_review_claim_heartbeat(
    owned_claim_args: dict[str, Any],
) -> ReviewClaimHeartbeat:
    """Start the background lease-renewal thread for an owned review claim."""
    stop = threading.Event()
    ownership_lost = threading.Event()
    thread = threading.Thread(
        target=_review_claim_heartbeat_loop,
        args=(owned_claim_args, stop, ownership_lost),
        name="review-claim-heartbeat",
        daemon=True,
    )
    thread.start()
    return ReviewClaimHeartbeat(
        stop=stop,
        ownership_lost=ownership_lost,
        thread=thread,
    )


def stop_review_claim_heartbeat(
    heartbeat: ReviewClaimHeartbeat | None,
) -> bool:
    """Stop the heartbeat thread. Returns True if ownership was retained."""
    if heartbeat is None:
        return True
    heartbeat.stop.set()
    heartbeat.thread.join(timeout=1.0)
    return not heartbeat.ownership_lost.is_set()


# ---------------------------------------------------------------------------
# Freshness (stale vs fresh) — base-insensitive identity check
# ---------------------------------------------------------------------------

_DIFF_FETCH_TIMEOUT = 15.0
_GH_API = "https://api.github.com"


def _fetch_current_review_snapshot(
    install_token: str, owner: str, repo: str, pull_number: int,
) -> tuple[str, str, str, bool]:
    """Read current snapshot identity, head SHA, state, and draft status."""
    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}",
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_DIFF_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        raise ValueError("GitHub PR response is not an object")
    head_sha = str((body.get("head") or {}).get("sha") or "")
    if not head_sha:
        raise ValueError("GitHub PR response has no head SHA")
    return (
        review_freshness_id_from_pr(body),
        head_sha,
        str(body.get("state") or ""),
        bool(body.get("draft", False)),
    )


def review_freshness_failure(
    *,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
    expected_snapshot_id: str,
    expected_head_sha: str,
) -> dict[str, str] | None:
    """Return a non-publishing result when a durable review input is stale.

    Returns None when the input is still fresh (safe to publish). Returns a
    result dict when the review should not publish (stale, ineligible, or
    GitHub unavailable).
    """
    try:
        (
            current_snapshot_id,
            current_head_sha,
            current_state,
            current_draft,
        ) = with_install_token_retry(
            installation_id,
            lambda token: _fetch_current_review_snapshot(
                token, owner, repo_name, pull_number,
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
        log.warning(
            "code_review_freshness_check_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        return {
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "freshness_check_failed",
        }
    if current_state != "open" or current_draft:
        log.info(
            "code_review_ineligible_before_publish",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "head_sha": current_head_sha[:8],
                "state": current_state,
                "draft": current_draft,
            },
        )
        return {
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "pr_ineligible",
        }
    if current_snapshot_id == expected_snapshot_id:
        return None
    # #773: HEAD SHA DECIDES, not the freshness id. The freshness id also
    # hashes title and body, so editing the PR body moved it while the code
    # under review was byte-identical - and this guard then threw the finished
    # review away.
    if expected_head_sha and expected_head_sha == current_head_sha:
        log.info(
            "code_review_intent_drift_publishing_anyway",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "head_sha": current_head_sha[:8],
                "reviewed_snapshot_id": expected_snapshot_id[:11],
                "current_snapshot_id": current_snapshot_id[:11],
            },
        )
        return None
    log.info(
        "code_review_stale_before_publish",
        extra={
            "installation_id": installation_id,
            "pr": f"{owner}/{repo_name}#{pull_number}",
            "reviewed_head_sha": expected_head_sha[:8],
            "current_head_sha": current_head_sha[:8],
            "reviewed_snapshot_id": expected_snapshot_id[:11],
            "current_snapshot_id": current_snapshot_id[:11],
        },
    )
    return {
        "persona": "code_reviewer",
        "result": "skipped",
        "degraded_reason": "stale_snapshot",
    }


# ---------------------------------------------------------------------------
# Staleness watch (mid-flight cancellation)
# ---------------------------------------------------------------------------

_STALENESS_WATCH_INTERVAL_S = 10.0


@dataclass(frozen=True, slots=True)
class StalenessWatch:
    stop: threading.Event
    cancel: threading.Event
    thread: threading.Thread


def _review_staleness_watch_loop(
    install_id: int,
    owner: str,
    repo_name: str,
    pr_number: int,
    expected_head_sha: str,
    stop: threading.Event,
    cancel: threading.Event,
    fetch_current_pr: Callable[..., dict[str, Any]],
) -> None:
    """Poll the PR every `_STALENESS_WATCH_INTERVAL_S` while Elder is
    actually generating a review.

    HEAD SHA DECIDES. Killing a running review is only justified when the
    diff changed, because that is the only case where the answer being
    generated is worthless. A transient GitHub hiccup is logged and skipped,
    not treated as staleness.
    """
    while not stop.wait(_STALENESS_WATCH_INTERVAL_S):
        try:
            latest = fetch_current_pr(
                install_id, owner, repo_name, pr_number,
            )
        except Exception as error:  # noqa: BLE001 - transient fetch failure
            log.warning(
                "elder_review_staleness_watch_fetch_failed",
                extra={
                    "repo": f"{owner}/{repo_name}",
                    "pr": pr_number,
                    "kind": type(error).__name__,
                },
            )
            continue
        latest_head = str((latest.get("head") or {}).get("sha") or "")
        if not latest_head:
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
                },
            )
            return


def start_staleness_watch(
    install_id: int,
    owner: str,
    repo_name: str,
    pr_number: int,
    expected_freshness_id: str,
    expected_head_sha: str,
    fetch_current_pr: Callable[..., dict[str, Any]],
) -> StalenessWatch:
    """Start the background staleness watcher for an in-flight review."""
    stop = threading.Event()
    cancel = threading.Event()
    thread = threading.Thread(
        target=_review_staleness_watch_loop,
        args=(
            install_id, owner, repo_name, pr_number,
            expected_head_sha, stop, cancel, fetch_current_pr,
        ),
        name="review-staleness-watch",
        daemon=True,
    )
    thread.start()
    return StalenessWatch(stop=stop, cancel=cancel, thread=thread)


def stop_staleness_watch(watch: StalenessWatch | None) -> None:
    if watch is None:
        return
    watch.stop.set()
    watch.thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Terminal transitions / redrive policy
# ---------------------------------------------------------------------------

# grug#773 AC3: a review discarded and re-enqueued against the SAME head_sha
# this many times running gives up with a terminal neutral check instead of
# looping forever. A genuinely new commit always resets this to 0; it only
# counts same-sha redrives.
_MAX_INTENT_DRIFT_REDRIVES = 3


@dataclass(frozen=True, slots=True)
class RedriveDecision:
    action: str  # "redrive" | "give_up" | "hold"
    next_redrive_count: int


def decide_redrive(
    *,
    same_sha: bool,
    job_redrive_count: int,
    max_intent_drift_redrives: int = _MAX_INTENT_DRIFT_REDRIVES,
    is_held_by_autopause: bool = False,
) -> RedriveDecision:
    """Pure decision: re-enqueue the stale snapshot, give up, or hold.

    Unit-testable with no mocks at all — no SQS, no Postgres, no GitHub.
    A genuinely new commit (not same_sha) always redrives with a fresh
    count; AC3 is about looping on an UNCHANGED sha.
    """
    next_redrive_count = job_redrive_count + 1 if same_sha else 0
    if same_sha and next_redrive_count > max_intent_drift_redrives:
        return RedriveDecision(action="give_up", next_redrive_count=next_redrive_count)
    if not same_sha and is_held_by_autopause:
        return RedriveDecision(action="hold", next_redrive_count=next_redrive_count)
    return RedriveDecision(action="redrive", next_redrive_count=next_redrive_count)


# ---------------------------------------------------------------------------
# Active claim registry (graceful-shutdown release)
# ---------------------------------------------------------------------------

# A consumer pod that dies mid-review orphans its snapshot claim: the
# in-function except/finally never runs, the lease outlives the pod by up to
# _REVIEW_CLAIM_LEASE_SECONDS, and the SQS redelivery bounces off "claim
# busy". The registry tracks every in-flight claim so the consumer can
# release them all on SIGTERM.
_ACTIVE_REVIEW_CLAIMS: dict[str, dict[str, Any]] = {}
_ACTIVE_REVIEW_CLAIMS_LOCK = threading.Lock()


def register_active_review_claim(
    token: str, owned_claim_args: dict[str, Any],
) -> None:
    with _ACTIVE_REVIEW_CLAIMS_LOCK:
        _ACTIVE_REVIEW_CLAIMS[token] = owned_claim_args


def unregister_active_review_claim(token: str) -> None:
    with _ACTIVE_REVIEW_CLAIMS_LOCK:
        _ACTIVE_REVIEW_CLAIMS.pop(token, None)


def release_active_review_claims() -> int:
    """Release every still-registered review claim (graceful shutdown).

    Best-effort per claim — one failed release must not stop the rest.
    Returns the number released."""
    with _ACTIVE_REVIEW_CLAIMS_LOCK:
        claims = list(_ACTIVE_REVIEW_CLAIMS.items())
        _ACTIVE_REVIEW_CLAIMS.clear()
    released = 0
    if not claims:
        return 0
    for _token, owned_claim_args in claims:
        try:
            if _adapter_release_review_claim(**owned_claim_args):
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
