"""Sentinel persona - flags PRs closed while Elder's review had unresolved
high/critical findings (grug#721, epic #707; extended grug#743).

v1 read ONLY Elder's stored CheckVerdictRecord for the PR's HEAD commit -
the same signal Elder derives from its own _BLOCKING_SEVERITIES. That has
a real blind spot (found in a 2026-07-24 live audit, grug#679/#743):
Elder's Living Hunt incremental review scopes each pass to the diff since
the last reviewed head, so the LAST verdict reflects only that pass's OWN
findings - a high/critical finding posted on an EARLIER pass that nobody
ever replied to silently drops out of the check conclusion once a later,
unrelated push comes back clean. grug#679 merged exactly this way: a
`high` SSRF finding sat with zero reply for a week, but the final "Grug -
Elder" check read `success` because the last push's delta didn't touch
those lines again.

v2 (this extension) ALSO checks every high/critical CommentRecord Elder
posted across the PR's WHOLE history for a human reply (a genuine per-
finding "was this addressed" proxy that needs no new GitHub capability -
REST review-comment `in_reply_to_id` chains, not GraphQL thread
`isResolved`, which this codebase has never called). Still deliberately
coarse: a real per-finding resolution lifecycle (open/addressed/
dismissed/outdated/superseded) is epic #732 Hunt Board's job, which this
can migrate onto once it ships. A CommentRecord's TTL is 30 days, so a PR
open longer than that loses its earliest findings' coverage here - an
accepted gap, not silently swallowed (logged when it matters).

Motivated by a real incident: grug#721 was closed unmerged over an
unresolved critical secret-in-log finding, and nobody noticed until a
manual audit days later. Registered with actions=("closed",), same seam as
Warder (#471) - but unlike Warder, this fires on EITHER outcome (merged or
not): a PR that merges while its blocking check wasn't a REQUIRED status
check is the worse case (the finding shipped), not a better one.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import httpx

from activity_log import record_check_verdict
from adapters.install_store import (
    CheckVerdictRecord, CommentRecord, get_check_verdict, list_comment_records_for_pr,
)
from github_app_auth import get_app_id, with_install_token_retry
from personas.registry import PullRequestContext

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.sentinel")

_API = "https://api.github.com"
_TIMEOUT = 10.0
_MAX_MARKER_SCAN_PAGES = 20
_MAX_COMMENT_PAGES = 20
# Same severities Elder's own dispatch treats as blocking
# (personas.code_reviewer.persona._BLOCKING_SEVERITIES) - duplicated as a
# literal rather than imported to avoid coupling Sentinel to Elder's
# internals across a persona boundary.
_HIGH_SEVERITY = frozenset(("high", "critical"))
MARKER = "<!-- grug-sentinel:abandoned-review -->"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo_path(owner: str, repo: str) -> str:
    return f"{quote(owner, safe='')}/{quote(repo, safe='')}"


def _find_marker_comment(
    token: str, owner: str, repo: str, pr_number: int,
) -> int | None:
    """Same own-app-id + marker check as Teller's upsert (walkthrough/
    dispatch.py) - a bare marker-substring match could be spoofed by any
    contributor typing the marker text into a comment."""
    own_app_id = get_app_id()
    page = 1
    while page <= _MAX_MARKER_SCAN_PAGES:
        resp = httpx.get(
            f"{_API}/repos/{_repo_path(owner, repo)}/issues/{pr_number}/comments",
            params={"per_page": 100, "page": page}, headers=_headers(token), timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        for c in batch:
            app = c.get("performed_via_github_app")
            if not app or str(app.get("id")) != own_app_id:
                continue
            if MARKER in (c.get("body") or ""):
                return int(c["id"])
        if len(batch) < 100:
            return None
        page += 1
    log.warning(
        "sentinel_marker_scan_capped",
        extra={"repo": f"{owner}/{repo}", "pr": pr_number},
    )
    return None


def _flag_once(token: str, owner: str, repo: str, pr_number: int, body: str) -> bool:
    """Post the flag comment iff one isn't already there for this PR.
    Returns True if newly posted, False if a marker comment already
    existed (redelivery, or a reopen/close cycle with no new commit)."""
    if _find_marker_comment(token, owner, repo, pr_number) is not None:
        return False
    httpx.post(
        f"{_API}/repos/{_repo_path(owner, repo)}/issues/{pr_number}/comments",
        json={"body": body}, headers=_headers(token), timeout=_TIMEOUT,
    ).raise_for_status()
    return True


def _high_severity_records(
    installation_id: int, owner: str, repo: str, pr_number: int,
) -> list[CommentRecord]:
    """This PR's CommentRecords whose finding severity is high/critical -
    the candidate set for the abandoned-finding check. Best-effort: a
    store hiccup returns [] (the existing last-verdict check still runs)."""
    try:
        records = list_comment_records_for_pr(installation_id, f"{owner}/{repo}", pr_number)
    except Exception as e:  # noqa: BLE001 - never fail Sentinel for this
        log.warning(
            "sentinel_comment_records_lookup_failed",
            extra={"repo": f"{owner}/{repo}", "pr": pr_number, "kind": type(e).__name__},
        )
        return []
    return [r for r in records if r.get("finding_tags", {}).get("severity") in _HIGH_SEVERITY]


def _replied_to_comment_ids(token: str, owner: str, repo: str, pr_number: int) -> set[int]:
    """Comment ids that have at least one HUMAN (non-bot) reply anywhere
    in their thread - the per-finding "was this addressed" proxy. Not the
    same as GitHub's thread `isResolved` (a human can reply substantively
    without ever clicking Resolve - observed live on grug PR #694/#739),
    so a reply is the more inclusive, REST-only signal."""
    out: set[int] = set()
    page = 1
    while page <= _MAX_COMMENT_PAGES:
        resp = httpx.get(
            f"{_API}/repos/{_repo_path(owner, repo)}/pulls/{pr_number}/comments",
            params={"per_page": 100, "page": page}, headers=_headers(token), timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        for c in batch:
            in_reply_to = c.get("in_reply_to_id")
            if in_reply_to is None:
                continue
            if str((c.get("user") or {}).get("type", "")) == "Bot":
                continue
            out.add(int(in_reply_to))
        if len(batch) < 100:
            return out
        page += 1
    log.warning(
        "sentinel_reply_scan_capped",
        extra={"repo": f"{owner}/{repo}", "pr": pr_number},
    )
    return out


def _abandoned_findings(
    token: str, owner: str, repo: str, pr_number: int, candidates: list[CommentRecord],
) -> list[CommentRecord]:
    replied = _replied_to_comment_ids(token, owner, repo, pr_number)
    return [r for r in candidates if r["comment_id"] not in replied]


def _build_comment(
    *, merged: bool, findings_count: int, elder_summary: str, abandoned_only: bool,
) -> str:
    if abandoned_only:
        # Last verdict itself was clean - the gap is an EARLIER pass's
        # finding that Living Hunt's incremental scoping dropped from the
        # conclusion once a later, unrelated push came back clean.
        return (
            f"{MARKER}\n"
            f"Grug Haunt notice: this PR was {'merged' if merged else 'closed'} "
            f"with {findings_count} high/critical Elder finding(s) from EARLIER in its "
            f"review that never got a reply.\n\n"
            f"The most recent Elder check on this PR was clean, but Living Hunt's "
            f"incremental scoping stopped surfacing an older finding once a later "
            f"push touched other lines - the check going green didn't mean the "
            f"finding was addressed. If this was already discussed and "
            f"intentionally left as-is, no action needed - this is a visibility "
            f"net, not a judgment.\n\n"
            f"<details><summary>Elder's last verdict summary</summary>\n\n"
            f"{elder_summary}\n\n</details>"
        )
    if merged:
        outcome = "merged with its blocking check still failing"
        note = "This means the finding(s) below may have SHIPPED - please verify."
    else:
        outcome = "closed without merging"
        note = (
            "If these were already discussed and intentionally left as-is, "
            "no action needed - this is a visibility net, not a judgment."
        )
    return (
        f"{MARKER}\n"
        # Do NOT claim a severity here. `findings_count` on this path is the
        # VERDICT's total finding count - any severity - while the abandoned
        # path above counts high/critical records specifically. Asserting
        # "severity high/critical" made one comment say both "0 blocking,
        # 1 total" (quoted from Elder, below) and "still blocking, severity
        # high/critical" about the same review (#775, infra#1998, where the
        # finding was `low | dead-code`).
        #
        # The real signal - a PR merged while its check was still blocking -
        # is worth keeping loud. Overstating the severity is what makes it
        # easy to dismiss, so state only what this path actually knows.
        f"Grug Haunt notice: this PR was {outcome} while Elder's last "
        f"review on this commit was still blocking "
        f"({findings_count} finding(s); see Elder's summary for severity)."
        f"\n\n"
        f"{note}\n\n"
        f"<details><summary>Elder's last verdict summary</summary>\n\n"
        f"{elder_summary}\n\n</details>"
    )


def _gather_flag_evidence(
    ctx: PullRequestContext, verdict: CheckVerdictRecord | None, verdict_blocking: bool,
) -> tuple[bool, int, str] | None:
    """Decide whether this PR should be flagged, and with what evidence.
    Returns None to skip, else (abandoned_only, findings_count, summary).
    Extracted from dispatch_pull_request to keep it under the complexity
    cap once the grug#743 abandoned-finding check was added alongside the
    original last-verdict check."""
    candidates = _high_severity_records(
        ctx.installation_id, ctx.owner, ctx.repo_name, ctx.pr_number,
    )
    if not verdict_blocking and not candidates:
        return None

    abandoned: list[CommentRecord] = []
    if candidates:
        try:
            abandoned = with_install_token_retry(
                ctx.installation_id,
                lambda token: _abandoned_findings(
                    token, ctx.owner, ctx.repo_name, ctx.pr_number, candidates,
                ),
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.warning(
                "sentinel_reply_scan_failed",
                extra={
                    "installation_id": ctx.installation_id,
                    "pr": f"{ctx.owner}/{ctx.repo_name}#{ctx.pr_number}",
                    "kind": type(e).__name__,
                },
            )
            # Best-effort: the last-verdict signal (if any) still applies.

    if not verdict_blocking and not abandoned:
        return None

    abandoned_only = not verdict_blocking
    findings_count = (
        int(verdict.get("findings_count") or 0) if verdict_blocking and verdict
        else len(abandoned)
    )

    # EVIDENCE GATE. `verdict_blocking` is the stored `blocking` flag, which is
    # independent of how many findings there were - it is also True when
    # Elder's judge HELD BACK weak findings, and when a degraded review failed
    # closed. In both cases nothing was published, so nothing can have shipped.
    #
    # Without this, Sentinel warned "this PR was merged ... (0 finding(s),
    # severity high/critical). This means the finding(s) below may have
    # SHIPPED" - an alert naming zero findings, whose own quoted evidence read
    # "Elder clear - weak markings held back". Observed live on
    # a private app repo.
    #
    # A safety net that fires with no evidence is worse than none: it is
    # indistinguishable from the real grug#721 case (a critical secret-in-log
    # closed unresolved) that this persona exists to catch, so it trains the
    # operator to ignore exactly the alert that matters.
    if findings_count <= 0 and not abandoned:
        log.info(
            "sentinel_skip_no_evidence",
            extra={
                "installation_id": ctx.installation_id,
                "pr": f"{ctx.owner}/{ctx.repo_name}#{ctx.pr_number}",
                "verdict_blocking": verdict_blocking,
                "reason": "blocking verdict but zero published findings "
                          "(judge held back, or degraded fail-closed)",
            },
        )
        return None
    summary = (verdict.get("summary") or "")[:500] if verdict else ""
    return abandoned_only, findings_count, summary


def dispatch_pull_request(ctx: PullRequestContext) -> dict[str, str]:
    pr = ctx.payload.get("pull_request") or {}
    merged = bool(pr.get("merged"))
    # Elder's stored verdict is keyed by the PR HEAD sha (what it actually
    # reviewed) - not the merge commit Warder anchors on for its own,
    # unrelated release-changelog purpose.
    head_sha = ctx.head_sha

    verdict = get_check_verdict(ctx.installation_id, head_sha, "elder")
    # The check GATED the merge only when it concluded `failure`. The stored
    # `blocking` field is the repo's review MODE (code_reviewer_blocking), so
    # reading it here flagged every merge in a blocking-mode repo that carried
    # a single medium advisory as "merged with its blocking check still
    # failing" - 30 such notices in the week to 2026-09-22, each over a
    # `success` check.
    verdict_blocking = bool(verdict and verdict.get("conclusion") == "failure")

    evidence = _gather_flag_evidence(ctx, verdict, verdict_blocking)
    if evidence is None:
        return {"persona": "sentinel", "result": "skipped"}
    abandoned_only, findings_count, summary = evidence

    body = _build_comment(
        merged=merged, findings_count=findings_count, elder_summary=summary,
        abandoned_only=abandoned_only,
    )

    try:
        posted = with_install_token_retry(
            ctx.installation_id,
            lambda token: _flag_once(token, ctx.owner, ctx.repo_name, ctx.pr_number, body),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "sentinel_publish_failed",
            extra={
                "installation_id": ctx.installation_id,
                "pr": f"{ctx.owner}/{ctx.repo_name}#{ctx.pr_number}",
                "kind": type(e).__name__,
            },
        )
        return {"persona": "sentinel", "result": "publish_failed"}

    if not posted:
        return {"persona": "sentinel", "result": "already_flagged"}

    log.warning(
        "review_abandoned_with_open_findings",
        extra={
            "installation_id": ctx.installation_id,
            "pr": f"{ctx.owner}/{ctx.repo_name}#{ctx.pr_number}",
            "head_sha": head_sha,
            "merged": merged,
            "findings_count": findings_count,
            "abandoned_only": abandoned_only,
        },
    )
    record_check_verdict(
        install_id=ctx.installation_id,
        persona_key="sentinel",
        repo=f"{ctx.owner}/{ctx.repo_name}",
        pr_number=ctx.pr_number,
        head_sha=head_sha,
        conclusion="neutral",
        summary=(
            f"PR {'merged' if merged else 'closed'} with {findings_count} "
            "blocking finding(s) still open"
        ),
        findings_count=findings_count,
        blocking=False,
    )
    return {"persona": "sentinel", "result": "flagged"}
