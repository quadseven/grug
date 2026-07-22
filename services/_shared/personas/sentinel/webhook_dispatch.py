"""Sentinel persona - flags PRs closed while Elder's last review verdict on
the final commit was still blocking (grug#721, epic #707).

v1 is deliberately coarse: it reads Elder's stored CheckVerdictRecord for
the PR's head commit and checks the persona's own `blocking` flag - the
same signal Elder already derives from _BLOCKING_SEVERITIES (high/critical
findings present). It does NOT track individual findings' resolution state
(no per-finding lifecycle exists yet - see epic #732 Hunt Board, which this
can migrate onto once it ships). Because of that, this is worded as a
VISIBILITY net, not an accusation: a PR whose findings were genuinely
discussed and declined would still trip this today, since a reply doesn't
clear `blocking` without a new commit re-triggering Elder. Precision
belongs to Hunt Board; recall belongs here.

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
from adapters.install_store import get_check_verdict
from github_app_auth import get_app_id, with_install_token_retry
from personas.registry import PullRequestContext

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.sentinel")

_API = "https://api.github.com"
_TIMEOUT = 10.0
_MAX_MARKER_SCAN_PAGES = 20
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


def _build_comment(*, merged: bool, findings_count: int, elder_summary: str) -> str:
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
        f"Grug Sentinel notice: this PR was {outcome} while Elder's last "
        f"review on this commit was still blocking ({findings_count} "
        f"finding(s), severity high/critical).\n\n"
        f"{note}\n\n"
        f"<details><summary>Elder's last verdict summary</summary>\n\n"
        f"{elder_summary}\n\n</details>"
    )


def dispatch_pull_request(ctx: PullRequestContext) -> dict[str, str]:
    pr = ctx.payload.get("pull_request") or {}
    merged = bool(pr.get("merged"))
    # Elder's stored verdict is keyed by the PR HEAD sha (what it actually
    # reviewed) - not the merge commit Warder anchors on for its own,
    # unrelated release-changelog purpose.
    head_sha = ctx.head_sha

    verdict = get_check_verdict(ctx.installation_id, head_sha, "elder")
    if verdict is None or not verdict.get("blocking"):
        return {"persona": "sentinel", "result": "skipped"}

    findings_count = int(verdict.get("findings_count") or 0)
    summary = (verdict.get("summary") or "")[:500]
    body = _build_comment(merged=merged, findings_count=findings_count, elder_summary=summary)

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
