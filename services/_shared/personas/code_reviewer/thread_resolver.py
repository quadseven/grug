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
              databaseId body path line originalLine
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


class GraphQLError(RuntimeError):
    """GitHub answered 200 with an `errors` array."""


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
        # Only the first message's presence matters; the text stays out of
        # logs because it can echo request content.
        raise GraphQLError("graphql returned errors")
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


def _owned_rule_and_line(thread: dict) -> tuple[str, str, int] | None:
    """`(rule, path, original_line)` when the thread is wholly Elder's and
    safe to close, else None."""
    comments = thread.get("comments") or {}
    nodes = comments.get("nodes") or []
    if not nodes or int(comments.get("totalCount") or 0) > len(nodes):
        return None
    if not all(_is_grug((c.get("author") or {}).get("login")) for c in nodes):
        return None
    first = nodes[0]
    rule = parse_rule(first.get("body") or "")
    line = first.get("originalLine") or first.get("line")
    if rule is None or not first.get("path") or not line:
        return None
    if not (first.get("originalCommit") or {}).get("oid"):
        log.info("elder_thread_resolve_skipped_history_gone",
                 extra={"thread_id": thread.get("id")})
        return None
    return rule, first["path"], int(line)


def select_fixed_threads(
    threads: list[dict], findings: tuple[Finding, ...],
) -> list[str]:
    """Node ids of threads that are Elder's, outdated, and not re-raised."""
    picked: list[str] = []
    for t in threads:
        if t.get("isResolved") or not t.get("isOutdated"):
            continue
        owned = _owned_rule_and_line(t)
        if owned is None:
            continue
        rule, path, line = owned
        if any(
            f.rule_name == rule and f.file == path
            and abs(f.line - line) <= _NEARBY_LINES
            for f in findings
        ):
            continue
        picked.append(t["id"])
    return picked


def resolve_fixed_threads(
    installation_id: int, owner: str, repo: str, pull_number: int,
    *, head_sha: str, findings: tuple[Finding, ...],
) -> int:
    """Reply "fixed in <sha>" and resolve each qualifying Elder thread.

    Returns the number resolved. Never raises.
    """
    pr_ref = f"{owner}/{repo}#{pull_number}"
    try:
        threads = with_install_token_retry(
            installation_id,
            lambda token: _fetch_threads(token, owner, repo, pull_number),
        )
        targets = select_fixed_threads(threads, findings)
    except Exception as e:  # noqa: BLE001 - never fail a review for tidying
        log.warning("elder_thread_list_failed",
                    extra={"pr": pr_ref, "kind": type(e).__name__})
        return 0
    body = f"Fixed in {head_sha[:7]}: the flagged code changed and this review does not raise it again."
    resolved = 0
    for thread_id in targets:
        try:
            with_install_token_retry(
                installation_id,
                lambda token, tid=thread_id: (
                    _graphql(token, _REPLY_MUTATION,
                             {"thread": tid, "body": body}),
                    _graphql(token, _RESOLVE_MUTATION, {"thread": tid}),
                ),
            )
            resolved += 1
        except Exception as e:  # noqa: BLE001 - one bad thread must not stop the rest
            log.warning("elder_thread_resolve_failed",
                        extra={"pr": pr_ref, "thread_id": thread_id,
                               "kind": type(e).__name__})
    if targets:
        log.info("elder_threads_resolved",
                 extra={"pr": pr_ref, "resolved": resolved,
                        "candidates": len(targets)})
    return resolved
