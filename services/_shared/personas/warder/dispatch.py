"""Warder persona dispatch (#471, epic #464 slice 7) - the release
manager TRACER: on a PR merged to the default branch, post a
"Grug - Warder" check-run with a drafted changelog (grouped from
Conventional-Commit prefixes since the last tag) and a semver hint.

Seam choice (the spec's "pick ONE"): the MERGED-PR event
(`pull_request` action=closed + merged=true) - it proves the first
non-update action seam (registry `actions` field) with zero new webhook
subscriptions, and the merge SHA gives the check-run a natural anchor.

Advisory-only tracer: no Release creation, default OFF per repo
(`warder_enabled`). Non-conventional repos degrade to a freeform "other"
group - never a crash.

Deploy gate (grug#533): on the SAME release event, `_gate_verdict` folds
an optional DD SLO/monitor read into this same check-run pass rather than
posting a second one. A second check-run under the "warder" persona key
would both escape `check_run_reconciler`'s per-persona sweep (one
`check_run_name` per registry entry) and clobber this pass's
`activity_log` row (upserts on (persona, head_sha)) - one pass, one
verdict, one row stays true to both. Advisory by default
(`warder_gate_blocking` per repo, resolved by the dispatch loop into
`ctx.blocking`); a query failure degrades silently and never itself
blocks a release.
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
from personas.warder.slo_gate import is_healthy, query_monitor_state
from secrets_loader import get_dd_api_key, get_dd_app_key, get_warder_slo_map

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
        return f"No commits found since `{since}`. Grug Totem rest easy."
    order = ("breaking",) + _GROUPS + ("other",)
    lines = [f"Grug Totem count the marks since `{since}`:"]
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


def _gate_verdict(
    owner: str, repo_name: str, *, blocking: bool,
) -> tuple[str, str | None, bool]:
    """Query the configured DD monitor (if any) for `owner/repo_name` and
    render the deploy-gate section of the check-run summary.

    Returns (markdown_to_append, conclusion_override, breached).
    `conclusion_override` is None whenever the gate has nothing to say (no
    monitor configured for this repo, creds absent, or the query
    degraded) OR the breach is advisory (not `blocking`) - the
    release-tracer's own conclusion stands unchanged in both cases. Only
    a successfully-read breach on a `blocking`-configured repo overrides
    to "failure". `breached` is True on any successfully-read breach
    (blocking or not) so the caller can mark it in `findings_count` -
    otherwise an advisory breach would read as a clean "pass" in the
    Activity feed (ADR-0003 "no lies")."""
    monitor_id = (get_warder_slo_map() or {}).get(f"{owner}/{repo_name}")
    if monitor_id is None:
        return "", None, False
    try:
        api_key, app_key = get_dd_api_key(), get_dd_app_key()
    except Exception as e:  # noqa: BLE001 — creds unconfigured = feature off
        log.info("warder_slo_gate_degraded", extra={"stage": "creds", "kind": type(e).__name__})
        return "", None, False
    if not (api_key and app_key):
        return "", None, False

    state = query_monitor_state(monitor_id, api_key, app_key)
    if state is None:
        return (
            f"\n\n---\nDeploy gate: could not read DD monitor `{monitor_id}` "
            "(query failed). This only counsel - the checker's own trouble "
            "never blocks a release.",
            None,
            False,
        )
    if is_healthy(state):
        return (
            f"\n\n---\nDeploy gate: DD monitor `{monitor_id}` is **{state}** - release clear.",
            None,
            False,
        )
    verb = "BLOCKS" if blocking else "advisory-blocks (warn only)"
    section = (
        f"\n\n---\nDeploy gate: DD monitor `{monitor_id}` is **{state}** (in breach) - "
        f"Warder {verb} the release."
    )
    return section, ("failure" if blocking else None), True


def dispatch_warder_release(
    *, installation_id: int, owner: str,
    repo_name: str, head_sha: str, pr_number: int, gate_blocking: bool = False,
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
        title = "WARN Warder eyes clouded"
        summary = (
            "Grug Totem could not read the marks (commit fetch failed). "
            "This only counsel — merge already done."
        )
        conclusion = "neutral"
        degraded_reason = "fetch_failed"

    gate_section, gate_conclusion, gate_breached = _gate_verdict(
        owner, repo_name, blocking=gate_blocking,
    )
    if gate_section:
        summary += gate_section
        if gate_conclusion == "failure":
            title += " — deploy gate FAILED"
        conclusion = gate_conclusion or conclusion

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
        findings_count=1 if gate_breached else 0,
        blocking=conclusion == "failure",
        degraded_reason=degraded_reason or ("check_publish_failed" if publish_failed else None),
    )
    if publish_failed:
        return {"persona": "warder", "result": "publish_failed"}
    if degraded_reason:
        return {"persona": "warder", "result": "skipped"}
    if conclusion == "failure":
        return {"persona": "warder", "result": "fail"}
    return {"persona": "warder", "result": "pass"}
