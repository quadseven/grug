"""Warder persona dispatch (#471, epic #464 slice 7) - the release
manager TRACER: on a PR merged to the default branch, post a
"Grug — Warder" check-run with a drafted changelog (grouped from
Conventional-Commit prefixes since the last tag) and a semver hint.

Seam choice (the spec's "pick ONE"): the MERGED-PR event
(`pull_request` action=closed + merged=true) - it proves the first
non-update action seam (registry `actions` field) with zero new webhook
subscriptions, and the merge SHA gives the check-run a natural anchor.

Advisory-only tracer: no deploy gating, no Release creation, default
OFF per repo (`warder_enabled`). Non-conventional repos degrade to a
freeform "other" group - never a crash.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote

import httpx

from activity_log import record_check_verdict
from github_app_auth import with_install_token_retry
from github_checks_client import CheckRunResult, post_check_run
from personas.tribe import CHECK_WARDER

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.warder")

_CHECK_NAME = CHECK_WARDER
_FETCH_TIMEOUT = 10
# Conventional-Commit prefixes we group by; anything else lands in
# "other" (the non-conventional degrade path).
_GROUPS = ("feat", "fix", "perf", "refactor", "docs", "test", "ci", "chore")
_CC_RE = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?:\s*(?P<desc>.+)$")
_MAX_COMMITS = 100


def group_commits(messages: tuple[str, ...]) -> dict[str, list[str]]:
    """Group commit SUBJECT lines by Conventional-Commit type. Pure.

    Unparseable subjects go to "other" (freeform repos never crash);
    a `!` bang or a BREAKING CHANGE body marker files under "breaking"
    (checked on the subject only - the tracer sees subject lines)."""
    groups: dict[str, list[str]] = {}
    for msg in messages:
        subject = msg.splitlines()[0].strip() if msg else ""
        if not subject:
            continue
        m = _CC_RE.match(subject)
        if m and (m.group("bang") or "BREAKING CHANGE" in msg):
            groups.setdefault("breaking", []).append(m.group("desc"))
        elif m and m.group("type") in _GROUPS:
            groups.setdefault(m.group("type"), []).append(m.group("desc"))
        else:
            groups.setdefault("other", []).append(subject)
    return groups


def semver_hint(groups: dict[str, list[str]]) -> str:
    """breaking -> major, feat -> minor, else patch. Pure."""
    if groups.get("breaking"):
        return "major"
    if groups.get("feat"):
        return "minor"
    return "patch"


def changelog_markdown(groups: dict[str, list[str]], *, since: str) -> str:
    """Render the drafted changelog section. Caveman-voiced header,
    conventional grouped body."""
    if not groups:
        return f"No commits found since `{since}`. Grug Warder rest easy."
    order = ("breaking",) + _GROUPS + ("other",)
    lines = [f"Grug Warder count the marks since `{since}`:"]
    titles = {
        "breaking": "BREAKING - tribe must know",
        "feat": "Features", "fix": "Fixes", "perf": "Performance",
        "refactor": "Refactoring", "docs": "Docs", "test": "Tests",
        "ci": "CI", "chore": "Chores", "other": "Other",
    }
    for key in order:
        items = groups.get(key)
        if not items:
            continue
        lines.append(f"\n### {titles[key]}")
        lines.extend(f"- {i}" for i in items)
    return "\n".join(lines)


def _fetch_commits_since_last_tag(
    token: str, owner: str, repo: str, head_sha: str,
) -> tuple[tuple[str, ...], str]:
    """(commit subject lines, since-label). Last tag via /tags (first
    entry = most recent); no tags -> the last _MAX_COMMITS commits
    (since-label "repo start"). Raises httpx errors - caller degrades."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    o, r = quote(owner, safe=""), quote(repo, safe="")
    resp = httpx.get(
        f"https://api.github.com/repos/{o}/{r}/tags",
        params={"per_page": 1}, headers=headers, timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    tags = resp.json() or []
    if tags:
        tag = tags[0]["name"]
        cmp = httpx.get(
            f"https://api.github.com/repos/{o}/{r}/compare/{quote(tag, safe='')}...{head_sha}",
            headers=headers, timeout=_FETCH_TIMEOUT,
        )
        cmp.raise_for_status()
        commits = (cmp.json() or {}).get("commits", [])[:_MAX_COMMITS]
        return tuple(c["commit"]["message"] for c in commits), tag
    listing = httpx.get(
        f"https://api.github.com/repos/{o}/{r}/commits",
        params={"sha": head_sha, "per_page": _MAX_COMMITS},
        headers=headers, timeout=_FETCH_TIMEOUT,
    )
    listing.raise_for_status()
    return tuple(c["commit"]["message"] for c in (listing.json() or [])), "repo start"


def dispatch_warder_release(
    *, installation_id: int, owner: str,
    repo_name: str, head_sha: str, pr_number: int,
) -> dict[str, str]:
    """One Warder pass on a merged PR. Never raises: fetch failures
    degrade to a neutral "eyes clouded" check-run + an errored Activity
    row (ADR-0003 "no lies")."""
    try:
        messages, since = with_install_token_retry(
            installation_id,
            lambda token: _fetch_commits_since_last_tag(
                token, owner, repo_name, head_sha,
            ),
        )
        groups = group_commits(messages)
        hint = semver_hint(groups)
        title = f"Warder draft the scroll — next mark looks {hint.upper()}"
        summary = (
            changelog_markdown(groups, since=since)
            + f"\n\nSemver hint: **{hint}** (breaking->major, feat->minor, else patch)."
        )
        conclusion = "neutral"  # advisory tracer - never gates
        degraded_reason = None
    except Exception as e:  # noqa: BLE001 — tracer must never break the dispatch loop
        log.warning(
            "warder_fetch_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pr_number}",
                "kind": type(e).__name__,
            },
        )
        title = "⚠️ Warder eyes clouded"
        summary = (
            "Grug Warder could not read the marks (commit fetch failed). "
            "This only counsel — merge already done."
        )
        conclusion = "neutral"
        degraded_reason = "fetch_failed"

    publish_failed = False
    try:
        with_install_token_retry(
            installation_id,
            lambda token: post_check_run(
                token, owner, repo_name,
                CheckRunResult(
                    name=_CHECK_NAME, head_sha=head_sha, status="completed",
                    conclusion=conclusion, title=title, summary=summary,
                ),
                external_id=f"grug-warder:{owner}/{repo_name}#{pr_number}:{head_sha}",
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            "warder_publish_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pr_number}",
                "kind": type(e).__name__,
            },
        )
        publish_failed = True

    record_check_verdict(
        install_id=installation_id,
        persona_key="warder",
        repo=f"{owner}/{repo_name}",
        pr_number=pr_number,
        head_sha=head_sha,
        conclusion=conclusion,
        summary=title,
        findings_count=0,
        blocking=False,
        degraded_reason=degraded_reason or ("check_publish_failed" if publish_failed else None),
    )
    if publish_failed:
        return {"persona": "warder", "result": "publish_failed"}
    if degraded_reason:
        return {"persona": "warder", "result": "skipped"}
    return {"persona": "warder", "result": "pass"}
