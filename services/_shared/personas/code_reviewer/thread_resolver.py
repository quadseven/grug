"""Resolve Elder's own review threads once a later push fixes the code (#1102).

Elder posts inline threads but never closed them, so a PR author faced a
wall of stale markings after fixing the code. After a new review, this
module resolves each thread that is provably Elder's, whose anchored code a
later commit changed or removed, and which the new review does not raise
again, replying "fixed in <sha>" first.

Ownership is conservative on every axis:
  - the thread's first comment must be authored by the grug app AND carry
    the hidden `grug-rule` marker;
  - any comment in the thread by anyone else (a person may be disagreeing)
    leaves it untouched, as does a comment list too long to inspect in full;
  - a reply from the PR author that starts with "Fixed in" is treated as
    the author confirming the fix, so the thread may still be resolved;
  - "code changed" is GitHub's own `isOutdated`, which flips only when a
    later commit changed or removed the anchored lines;
  - a thread whose original commit is gone (force-push) is skipped.

Entry point: `resolve_fixed_threads`. It never raises; every failure is
logged with its exception kind and the review outcome is unaffected.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from github_app_auth import with_install_token_retry
from personas.code_reviewer.dedup import parse_rule
from personas.code_reviewer.persona import Finding

log = logging.getLogger(
    f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.thread_resolver"
)

_GRAPHQL_URL = "https://api.github.com/graphql"
_TIMEOUT = 10
_MAX_THREAD_PAGES = 5
_COMMENTS_PER_THREAD = 20
# A re-raised finding counts as "the same" when it lands this close to the
# thread's original line: pushes shift lines, so exact equality under-matches
# and an under-match would resolve a thread the review just raised again.
_NEARBY_LINES = 10
# GraphQL reports an app author without the REST `[bot]` suffix.
_BOT_LOGIN = "grug-tribe"

_LIST_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $after: String,
      $perThread: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated
          comments(first: $perThread) {
            totalCount
            nodes {
              id databaseId isMinimized url body path line originalLine
              author { login }
              originalCommit { oid }
            }
          }
        }
      }
    }
  }
}
"""
_REPLY_MUTATION = """
mutation($thread: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {
    pullRequestReviewThreadId: $thread, body: $body
  }) { comment { id } }
}
"""
_RESOLVE_MUTATION = """
mutation($thread: ID!) {
  resolveReviewThread(input: {threadId: $thread}) { thread { isResolved } }
}
"""


_MINIMIZE_MUTATION = """
mutation($subject: ID!) {
  minimizeComment(input: {subjectId: $subject, classifier: OUTDATED}) {
    minimizedComment { isMinimized }
  }
}
"""


class GraphQLError(RuntimeError):
    """GitHub answered 200 with an `errors` array. `types` holds GitHub's
    error type codes (e.g. FORBIDDEN), never the messages, which can echo
    request content."""

    def __init__(self, types: tuple[str, ...] = ()):
        super().__init__("graphql returned errors: " + ",".join(types))
        self.types = types


def _graphql(token: str, query: str, variables: dict[str, Any]) -> dict:
    resp = httpx.post(
        _GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        # Messages stay out of logs because they can echo request content;
        # the type codes are safe and say what went wrong.
        raise GraphQLError(tuple(
            str(e.get("type") or "") for e in payload["errors"] if isinstance(e, dict)
        ))
    return payload.get("data") or {}


def _is_grug(login: str | None) -> bool:
    return (login or "").removesuffix("[bot]") == _BOT_LOGIN


def _fetch_threads(token: str, owner: str, repo: str, pr: int) -> list[dict]:
    out: list[dict] = []
    after: str | None = None
    for _ in range(_MAX_THREAD_PAGES):
        data = _graphql(token, _LIST_QUERY, {
            "owner": owner, "repo": repo, "pr": pr, "after": after,
            "perThread": _COMMENTS_PER_THREAD,
        })
        conn = (((data.get("repository") or {}).get("pullRequest") or {})
                .get("reviewThreads") or {})
        out.extend(conn.get("nodes") or [])
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
    return out


def _only_grug_comments(thread: dict, pr_author: str | None = None) -> list[dict] | None:
    """The thread's comments when the first is Elder's, the list is
    complete, and any remaining comments are either Elder or the PR author
    replying "Fixed in" (case-insensitive). Returns None when a person may
    be disagreeing or we cannot see all comments."""
    comments = thread.get("comments") or {}
    nodes = comments.get("nodes") or []
    if not nodes or int(comments.get("totalCount") or 0) > len(nodes):
        return None
    if not _is_grug((nodes[0].get("author") or {}).get("login")):
        return None
    for c in nodes[1:]:
        login = (c.get("author") or {}).get("login")
        if _is_grug(login):
            continue
        # A deleted account's comment has no login; without the `not
        # pr_author` check it would match an unknown author (None == None).
        if not pr_author or login != pr_author:
            return None
        # GitHub may send a null body; `or ""` keeps `.lower()` safe and
        # makes such a reply block like any non-"Fixed in" reply.
        body = (c.get("body") or "").lower()
        if not body.startswith(_FIXED_REPLY_PREFIX.lower()):
            return None
    return nodes


def _owned_rule_and_line(thread: dict, pr_author: str | None = None) -> tuple[str, str, int] | None:
    """`(rule, path, original_line)` when the thread is Elder's (or the
    author has already answered "Fixed in") and safe to close, else None."""
    nodes = _only_grug_comments(thread, pr_author)
    if nodes is None:
        return None
    first = nodes[0]
    if first.get("isMinimized"):
        # Collapsed by an earlier pass (the resolve fallback): done.
        return None
    rule = parse_rule(first.get("body") or "")
    line = first.get("originalLine") or first.get("line")
    if rule is None or not first.get("path") or not line:
        return None
    if not (first.get("originalCommit") or {}).get("oid"):
        log.info("elder_thread_resolve_skipped_history_gone",
                 extra={"thread_id": thread.get("id")})
        return None
    return rule, first["path"], int(line)


_FIXED_REPLY_PREFIX = "Fixed in "


def _already_answered(thread: dict, head_sha: str) -> bool:
    """An earlier pass (or the PR author) posted a "Fixed in" reply but
    the resolve call failed or has not run yet.

    Counting the author's reply here is deliberate (#1147): only threads
    that passed `select_fixed_threads` reach this check, so every non-Elder
    reply is the PR author's own "Fixed in". A second "Fixed in" from Elder
    would add noise, and the resolve still requires GitHub's `isOutdated`
    and no nearby re-raise, so the author's word alone never closes a thread.
    Pinned by `test_author_fixed_in_reply_is_not_answered_twice`."""
    nodes = (thread.get("comments") or {}).get("nodes") or []
    prefix = _FIXED_REPLY_PREFIX.lower()
    expected = head_sha[:7].lower()
    return any(
        (c.get("body") or "").lower().startswith(f"{prefix}{expected}:")
        for c in nodes[1:]
    )


def _blocked_by_person_reply(thread: dict, pr_author: str | None) -> bool:
    """True when a non-author person has commented, making the thread unsafe
    to close automatically."""
    comments = thread.get("comments") or {}
    nodes = comments.get("nodes") or []
    if not nodes or int(comments.get("totalCount") or 0) > len(nodes):
        return False
    if not _is_grug((nodes[0].get("author") or {}).get("login")):
        return True
    prefix = _FIXED_REPLY_PREFIX.lower()
    for c in nodes[1:]:
        login = (c.get("author") or {}).get("login")
        if _is_grug(login):
            continue
        if not pr_author or login != pr_author:
            return True
        if not (c.get("body") or "").lower().startswith(prefix):
            return True
    return False


def select_fixed_threads(
    threads: list[dict], findings: tuple[Finding, ...],
    *, pr_author: str | None = None,
) -> tuple[list[str], int, int]:
    """Node ids of threads that are Elder's, outdated, and not re-raised,
    plus skip counts for person replies and re-raised findings."""
    picked: list[str] = []
    skipped_person = 0
    skipped_reraised = 0
    for t in threads:
        if t.get("isResolved") or not t.get("isOutdated"):
            continue
        owned = _owned_rule_and_line(t, pr_author)
        if owned is None:
            if _blocked_by_person_reply(t, pr_author):
                skipped_person += 1
            continue
        rule, path, line = owned
        if any(
            f.rule_name == rule and f.file == path
            and abs(f.line - line) <= _NEARBY_LINES
            for f in findings
        ):
            skipped_reraised += 1
            continue
        picked.append(t["id"])
    return picked, skipped_person, skipped_reraised


def _resolve_or_collapse(
    installation_id: int, thread_id: str, comment_id: str | None,
) -> str:
    """Resolve the thread; if GitHub forbids it, collapse Elder's comment.

    GitHub answers `FORBIDDEN: Resource not accessible by integration` when
    the app lacks the permission resolving needs (seen live 2026-10-05),
    but an app may always minimize its own comment. Collapsing it as
    OUTDATED, under the "Fixed in" reply, is the same signal to the author.

    Returns ``resolved`` or ``collapsed``.
    """
    try:
        with_install_token_retry(
            installation_id,
            lambda tok: _graphql(tok, _RESOLVE_MUTATION, {"thread": thread_id}),
        )
        return "resolved"
    except GraphQLError as e:
        if "FORBIDDEN" not in e.types or not comment_id:
            raise
    with_install_token_retry(
        installation_id,
        lambda tok: _graphql(tok, _MINIMIZE_MUTATION, {"subject": comment_id}),
    )
    log.info("elder_thread_collapsed_not_resolved", extra={"thread_id": thread_id})
    return "collapsed"


def resolve_fixed_threads(
    installation_id: int, owner: str, repo: str, pull_number: int,
    *, head_sha: str, findings: tuple[Finding, ...],
    threads: list[dict] | None = None,
    pr_author: str | None = None,
) -> int:
    """Reply "fixed in <sha>" and resolve each qualifying Elder thread.

    `threads` is an already-fetched listing (shared with the carry-forward
    pass so a review lists once); None fetches it here. `pr_author` lets
    threads the PR author already answered "Fixed in" be closed without a
    second reply. Returns the number of threads touched. Never raises.
    """
    pr_ref = f"{owner}/{repo}#{pull_number}"
    try:
        if threads is None:
            threads = with_install_token_retry(
                installation_id,
                lambda tok: _fetch_threads(tok, owner, repo, pull_number),
            )
        targets, skipped_person, skipped_reraised = select_fixed_threads(
            threads, findings, pr_author=pr_author,
        )
        answered = {
            t["id"] for t in threads
            if t.get("id") in targets and _already_answered(t, head_sha)
        }
        first_comment_ids = {
            t["id"]: ((t.get("comments") or {}).get("nodes") or [{}])[0].get("id")
            for t in threads if t.get("id") in targets
        }
    except Exception as e:  # noqa: BLE001 - never fail a review for tidying
        log.warning("elder_thread_list_failed",
                    extra={"pr": pr_ref, "kind": type(e).__name__})
        return 0
    body = (
        f"{_FIXED_REPLY_PREFIX}{head_sha[:7]}: the flagged code changed and "
        "this review does not raise it again."
    )
    resolved = 0
    collapsed = 0
    failed = 0
    for thread_id in targets:
        try:
            # Reply and resolve are separate calls, each retried on its own,
            # so a token retry never re-posts the reply, and a thread whose
            # earlier resolve failed is resolved without a second reply.
            if thread_id not in answered:
                with_install_token_retry(
                    installation_id,
                    lambda tok, tid=thread_id: _graphql(
                        tok, _REPLY_MUTATION, {"thread": tid, "body": body},
                    ),
                )
            outcome = _resolve_or_collapse(
                installation_id, thread_id, first_comment_ids.get(thread_id),
            )
            if outcome == "resolved":
                resolved += 1
            else:
                collapsed += 1
        except Exception as e:  # noqa: BLE001 - one bad thread must not stop the rest
            failed += 1
            log.warning("elder_thread_resolve_failed",
                        extra={"pr": pr_ref, "thread_id": thread_id,
                               "kind": type(e).__name__})
    log.info("elder_threads_resolved",
             extra={"pr": pr_ref, "resolved": resolved, "collapsed": collapsed,
                    "skipped_person": skipped_person,
                    "skipped_reraised": skipped_reraised,
                    "candidates": len(targets), "failed": failed})
    return resolved + collapsed
