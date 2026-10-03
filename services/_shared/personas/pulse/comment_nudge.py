"""Pulse comment-nudge (#656, epic #654) - suggested-fix replies on stale threads.

Sibling to `personas/pulse/nudge.py` (#472), same shape: a scheduled
(non-webhook) pass riding the `grug-poller` CronJob cadence, store-driven
per-repo targeting, best-effort per repo, hard-capped per run.

For each opted-in repo's open PRs, fetches the three comment surfaces,
finds review threads with no reply from the PR author after `_STALE_HOURS`,
and posts ONE suggested-fix reply per thread (idempotent via the marker).
Never pushes a commit, never edits the PR, never merges, never replies as
the PR author - Grug's own advisory voice.

Default OFF per repo (`pulse_comment_nudge_enabled`).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(
    f"{os.getenv('DD_SERVICE', 'grug')}.persona.pulse.comment_nudge"
)

_STALE_HOURS = 72
_MAX_NUDGES_PER_INSTALL_RUN = 3  # mirrors Pulse's cap
_MAX_PRS_PER_REPO = 20
_MAX_REPOS_PER_INSTALL = 30
_FETCH_TIMEOUT = 10
_MARKER_PREFIX = "<!-- grug-pulse-comment-nudge:{comment_id} -->"
_BOT_LOGINS = frozenset({"grug-tribe[bot]", "github-actions[bot]"})


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def find_stale_threads(
    threads: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Threads needing a nudge: old, no author reply, no existing marker.

    A thread dict carries: root_id, root_body, root_created (iso),
    root_author, pr_author, replies=[{author, created, body}].
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=_STALE_HOURS)
    out = []
    for t in threads:
        try:
            root_ts = _parse_ts(t["root_created"])
        except (KeyError, ValueError):
            continue
        if root_ts > cutoff:
            continue
        pr_author = t.get("pr_author", "")
        replies = t.get("replies", [])
        # Author replied -> not stale. Existing nudge marker -> already nudged.
        if any(r.get("author") == pr_author for r in replies):
            continue
        if any(_MARKER_PREFIX.split("{")[0] in (r.get("body") or "") for r in replies):
            continue
        if _MARKER_PREFIX.split("{")[0] in (t.get("root_body") or ""):
            continue
        out.append(t)
    return out


def _extract_suggested_fix(body: str) -> str | None:
    """Pull the concrete fix out of an Elder-style marking, if present."""
    m = re.search(r"```suggestion\n(.*?)```", body, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"\*\*Fix\*\*.*?\n\n(.+?)(?:\n\n|\Z)", body, re.DOTALL)
    if m:
        return m.group(1).strip()[:500]
    return None


def _nudge_body(thread: dict[str, Any]) -> str:
    """One suggested-fix reply: concrete, not a vague 'please address this'."""
    fix = _extract_suggested_fix(thread.get("root_body", ""))
    marker = _MARKER_PREFIX.format(comment_id=thread["root_id"])
    if fix:
        proposal = (
            f"Grug nudge - this thread sleep {_STALE_HOURS} sunrises with no "
            f"reply. The marking proposed this fix:\n\n```\n{fix}\n```\n\n"
            f"A human or agent may take it or leave it - no commit was pushed."
        )
    else:
        snippet = (thread.get("root_body") or "")[:300].strip()
        proposal = (
            f"Grug nudge - this thread sleep {_STALE_HOURS} sunrises with no "
            f"reply. The ask was:\n\n> {snippet}\n\n"
            f"If the hunt is dead, say so and close the thread; if it lives, "
            f"a concrete fix proposal is welcome - no commit was pushed."
        )
    return f"{proposal}\n\n{marker}"


def _group_inline_comments(
    comments: list[dict[str, Any]], pr_author: str
) -> list[dict[str, Any]]:
    """Group review comments into threads via in_reply_to_id."""
    by_id = {c["id"]: c for c in comments}
    roots: dict[int, dict[str, Any]] = {}
    for c in comments:
        parent = c.get("in_reply_to_id")
        if parent and parent in by_id:
            root = by_id[parent]
            # Walk up to the true root.
            while root.get("in_reply_to_id") and root["in_reply_to_id"] in by_id:
                root = by_id[root["in_reply_to_id"]]
            rid = root["id"]
        else:
            rid = c["id"]
            root = c
        if rid not in roots:
            roots[rid] = {
                "root_id": rid,
                "root_body": root.get("body", ""),
                "root_created": root.get("created_at", ""),
                "root_author": (root.get("user") or {}).get("login", ""),
                "pr_author": pr_author,
                "replies": [],
                "kind": "inline",
                "pr_number": None,
            }
        if c["id"] != rid:
            roots[rid]["replies"].append(
                {
                    "author": (c.get("user") or {}).get("login", ""),
                    "created": c.get("created_at", ""),
                    "body": c.get("body", ""),
                }
            )
    return list(roots.values())


def _threads_for_pr(
    client: Any,
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    pr_author: str,
) -> list[dict[str, Any]]:
    """Fetch the three comment surfaces and normalize to threads."""
    headers = _headers(token)
    base = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    )
    threads: list[dict[str, Any]] = []

    # 1. Inline review comments (threaded).
    try:
        r = client.get(
            f"{base}/pulls/{pr_number}/comments",
            params={"per_page": 100},
            headers=headers,
            timeout=_FETCH_TIMEOUT,
        )
        r.raise_for_status()
        inline = _group_inline_comments(r.json(), pr_author)
        for t in inline:
            t["pr_number"] = pr_number
        threads.extend(inline)
    except Exception as e:  # noqa: BLE001 - one surface failing must not kill the pass
        log.info(
            "comment_nudge_surface_failed",
            extra={"surface": "inline", "pr": pr_number, "kind": type(e).__name__},
        )

    # 2. Review bodies (each review with a body is a thread).
    try:
        r = client.get(
            f"{base}/pulls/{pr_number}/reviews",
            params={"per_page": 100},
            headers=headers,
            timeout=_FETCH_TIMEOUT,
        )
        r.raise_for_status()
        for rev in r.json():
            if not (rev.get("body") or "").strip():
                continue
            threads.append(
                {
                    "root_id": rev["id"],
                    "root_body": rev.get("body", ""),
                    "root_created": rev.get("submitted_at", ""),
                    "root_author": (rev.get("user") or {}).get("login", ""),
                    "pr_author": pr_author,
                    "replies": [],
                    "kind": "review",
                    "pr_number": pr_number,
                }
            )
    except Exception as e:  # noqa: BLE001 - see above
        log.info(
            "comment_nudge_surface_failed",
            extra={"surface": "reviews", "pr": pr_number, "kind": type(e).__name__},
        )

    # 3. Issue-level PR comments (linear; each is its own thread).
    try:
        r = client.get(
            f"{base}/issues/{pr_number}/comments",
            params={"per_page": 100},
            headers=headers,
            timeout=_FETCH_TIMEOUT,
        )
        r.raise_for_status()
        for c in r.json():
            threads.append(
                {
                    "root_id": c["id"],
                    "root_body": c.get("body", ""),
                    "root_created": c.get("created_at", ""),
                    "root_author": (c.get("user") or {}).get("login", ""),
                    "pr_author": pr_author,
                    "replies": [],
                    "kind": "issue",
                    "pr_number": pr_number,
                }
            )
    except Exception as e:  # noqa: BLE001 - see above
        log.info(
            "comment_nudge_surface_failed",
            extra={"surface": "issue", "pr": pr_number, "kind": type(e).__name__},
        )
    return threads


def _post_nudge(
    client: Any,
    token: str,
    owner: str,
    repo: str,
    thread: dict[str, Any],
) -> bool:
    """Post the nudge reply. Returns True on success. Never merges/pushes."""
    headers = _headers(token)
    base = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    )
    body = _nudge_body(thread)
    pr_number = thread["pr_number"]
    try:
        if thread["kind"] == "inline":
            # Reply in the review thread.
            r = client.post(
                f"{base}/pulls/{pr_number}/comments",
                headers=headers,
                timeout=_FETCH_TIMEOUT,
                json={"body": body, "in_reply_to": thread["root_id"]},
            )
        else:
            # Review bodies and issue comments: issue-level reply.
            r = client.post(
                f"{base}/issues/{pr_number}/comments",
                headers=headers,
                timeout=_FETCH_TIMEOUT,
                json={"body": body},
            )
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001 - best-effort per thread
        log.info(
            "comment_nudge_post_failed",
            extra={
                "pr": pr_number,
                "thread_id": thread["root_id"],
                "kind": type(e).__name__,
            },
        )
        return False


def run_comment_nudge_for_install(
    token: str,
    install_id: int,
    repos: list[dict[str, Any]],
    *,
    http_client: Any | None = None,
    now: datetime | None = None,
) -> int:
    """Entry point: nudge stale threads across opted-in repos. Returns nudges posted."""
    client = http_client or httpx
    now = now or datetime.now(timezone.utc)
    nudged = 0
    for repo in repos[:_MAX_REPOS_PER_INSTALL]:
        if nudged >= _MAX_NUDGES_PER_INSTALL_RUN:
            break
        full = repo.get("full_name", "")
        if "/" not in full:
            continue
        owner, repo_name = full.split("/", 1)
        try:
            r = client.get(
                f"https://api.github.com/repos/{quote(owner, safe='')}"
                f"/{quote(repo_name, safe='')}/pulls",
                params={"state": "open", "per_page": _MAX_PRS_PER_REPO},
                headers=_headers(token),
                timeout=_FETCH_TIMEOUT,
            )
            r.raise_for_status()
            prs = r.json()
        except Exception as e:  # noqa: BLE001 - one repo's failure never aborts the run
            log.info(
                "comment_nudge_repo_failed",
                extra={"repo": full, "kind": type(e).__name__},
            )
            continue
        for pr in prs:
            if nudged >= _MAX_NUDGES_PER_INSTALL_RUN:
                break
            pr_number = int(pr.get("number", 0))
            pr_author = (pr.get("user") or {}).get("login", "")
            try:
                threads = _threads_for_pr(
                    client, token, owner, repo_name, pr_number, pr_author
                )
            except Exception as e:  # noqa: BLE001 - see above
                log.info(
                    "comment_nudge_pr_failed",
                    extra={"pr": pr_number, "kind": type(e).__name__},
                )
                continue
            for thread in find_stale_threads(threads, now=now):
                if nudged >= _MAX_NUDGES_PER_INSTALL_RUN:
                    break
                if _post_nudge(client, token, owner, repo_name, thread):
                    nudged += 1
                    log.info(
                        "comment_nudge_posted",
                        extra={
                            "repo": full,
                            "pr": pr_number,
                            "thread_id": thread["root_id"],
                        },
                    )
    return nudged
