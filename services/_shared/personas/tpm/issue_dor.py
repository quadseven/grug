"""Chief at ISSUE time - the DoR gate moved to where it is cheap to act on.

Grug has always been a PR-time bot. Every persona wakes on `pull_request`
or a PR comment, so Chief's Definition-of-Ready rules only reach a ticket
AFTER someone has written the code against it. By then a missing `## Why`
or absent acceptance criteria costs a rewrite, not a sentence.

This runs the SAME rules when the issue is filed, where the fix is free.
It is a new SURFACE for an existing persona, not a new persona: the rules
are imported from `dor_checks` verbatim, so PR-time and issue-time can
never disagree about what "ready" means. That is the whole point - a
second copy of the rules would drift and then Chief would contradict
himself across surfaces.

TWO CHECKS ARE DELIBERATELY EXCLUDED (`_ISSUE_CHECKS` below):

  - `check_issue_link` requires `closes #N`. That is a PR asking "what
    ticket does this close?". An issue closing another issue is not the
    normal shape, and demanding it would fail nearly every ticket.
  - `check_linked_issue_completeness` walks a PR's linked issues. At
    issue time there is no PR and nothing to walk.

Including either would make Chief nag about something structurally
impossible to satisfy, which trains people to ignore him - the failure
mode worth avoiding most, because an advisory nobody reads is worse than
no advisory (it still costs a notification).

ADVISORY ONLY. It never blocks, never closes, never edits the issue body,
and never posts a check-run - an issue has no head SHA to attach one to.
The author stays in control; Chief just says what is missing.

IDEMPOTENT + SELF-HEALING via an HTML marker:
  - first pass with gaps  -> post one comment
  - later pass with gaps  -> EDIT that comment in place, never a second one
  - later pass, now clean -> EDIT it to a short "ready" note
  - clean from the start  -> post NOTHING at all

Editing rather than appending is what makes this safe on the `edited`
event: a chatty author who saves five times gets one comment, not five.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import httpx

from personas.tpm.dor_checks import (
    CheckResult, IssueFactsFetcher, belongs_to_epic, check_acceptance,
    check_estimate, check_scope_fence, check_why, is_epic,
)

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.chief.issue_dor")

_FETCH_TIMEOUT = 10
_MARKER = "<!-- grug-chief-issue-dor -->"

# The four rules that describe a READY TICKET. See the module docstring for
# why the two PR-shaped checks in `dor_checks.ALL_CHECKS` are not here.
_ISSUE_CHECKS = (check_why, check_acceptance, check_estimate, check_scope_fence)

# What each failing check should tell the author to DO. The check's own
# `detail` says what is wrong ("## Why has 2 words; need >=5"); this says
# how to fix it. A gate that only reports is a gate people route around.
_REMEDY = {
    "why": "Add a `## Why` (or `## Summary`) section of at least 5 words - "
           "what breaks or costs today, not what to build.",
    "acceptance": "Add `## Acceptance criteria` (or `## Test plan`) with at "
                  "least 3 bullets, each one checkable by someone who did "
                  "not write the ticket.",
    "estimate": "Add a `Size: XS|S|M|L` line. XL is not accepted - split it.",
    "scope-fence": "Add an `## Out of scope` section. Naming what this is "
                   "NOT is what stops a slice growing while it is built.",
    "epic": "Add this ticket as a sub-issue of an epic, or write "
            "`Part of #<epic>` in its body. A PR closing it cannot merge "
            "until it has one (grug#1034).",
}


def _emit_epic_fetch_failed() -> None:
    """Best-effort gauge when the epic facts cannot be read (Grug Elder on
    #1038), the same shape as ticket-compliance's `_emit_metric`: a skipped
    epic line is silent in the comment, so the count is what shows an outage."""
    try:
        from observability import emit_gauge  # type: ignore
        emit_gauge("grug.chief.issue_dor.epic_fetch_failed", 1)
    except Exception as exc:  # noqa: BLE001 - telemetry never breaks the advisory
        # LOGGED, not passed (ruff S110 via Grug on #1038): a broken emitter or
        # import path must be visible, just never fatal to the comment.
        log.warning(
            "issue_dor_epic_gauge_failed",
            extra={"error_type": type(exc).__name__, "error": str(exc)},
        )


def check_issue_epic(issue_number: int, fetch_facts: IssueFactsFetcher | None) -> CheckResult | None:
    """The ticket itself is an epic or belongs to one (grug#1035).

    The SAME predicates as the PR-time `which epic` check (`is_epic`,
    `belongs_to_epic`), so the two surfaces cannot disagree. Returns None -
    no line at all - when there is no fetcher or the fetch fails: this
    surface is advisory, and a GitHub blip must not become a nag.
    """
    if fetch_facts is None:
        return None
    try:
        facts = fetch_facts(issue_number)
    except (httpx.HTTPError, ValueError) as exc:
        # NARROW on purpose (Grug Elder on #1038): a GitHub status error, a
        # transport failure or an unparseable body falls open; a TypeError or
        # NameError of our own must still surface as the bug it is.
        log.warning(
            "issue_dor_epic_fetch_failed",
            extra={"issue": issue_number, "error": str(exc)},
        )
        _emit_epic_fetch_failed()
        return None
    if is_epic(facts):
        return CheckResult("epic", True, "this ticket is an epic")
    if belongs_to_epic(facts):
        return CheckResult("epic", True, "belongs to an epic")
    return CheckResult("epic", False, "belongs to no epic")


def evaluate_issue(body: str, epic: CheckResult | None = None) -> list[CheckResult]:
    """Run the issue-time DoR subset over an issue body. Pure. `epic` is the
    already-evaluated epic line (`check_issue_epic`), appended when present."""
    results = [check(body or "") for check in _ISSUE_CHECKS]
    if epic is not None:
        results.append(epic)
    return results


def advisory_markdown(results: list[CheckResult]) -> str | None:
    """The comment body for a ticket with gaps, or None when it is ready.

    Returning None for a clean issue is load-bearing: it is what stops
    Chief congratulating people on well-formed tickets, which would be
    pure notification cost with no information.
    """
    failed = [r for r in results if not r.passed]
    if not failed:
        return None
    lines = [
        "Grug Chief read this ticket before hunt start. Tracks not clear yet -",
        "fix now while it cost one sentence, not after code written.",
        "",
        "| Missing | What Grug see | What fix it |",
        "|---|---|---|",
    ]
    for r in failed:
        lines.append(f"| `{r.name}` | {r.detail} | {_REMEDY.get(r.name, '')} |")
    passed = [r.name for r in results if r.passed]
    good = ", ".join(f"`{p}`" for p in passed) if passed else "nothing yet"
    lines += [
        "",
        f"Already good: {good}.",
        "",
        "Advisory only - Grug not block, not close, not edit ticket. Same rules "
        "Chief use on pull request, just earlier, where they cheap to fix. "
        "Edit the issue and Grug update this comment in place.",
        "",
        _MARKER,
    ]
    return "\n".join(lines)


def ready_markdown() -> str:
    """Replacement body once a previously-flagged ticket passes. Keeps the
    trail (an empty edit would read as Chief giving up) without nagging."""
    return "\n".join([
        "Grug Chief look again. Tracks clear now - ticket ready for hunt.",
        "",
        _MARKER,
    ])


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _existing_comment(
    token: str, owner: str, repo: str, issue_number: int,
) -> int | None:
    """Chief's own prior advisory on this issue, identified by the BODY
    MARKER rather than the author login. Author-matching would break the
    moment the App is renamed or the comment is posted by a different
    installation identity; the marker is the identity."""
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/issues/{issue_number}/comments",
        params={"per_page": 100},
        headers=_headers(token),
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    for c in resp.json() or []:
        if _MARKER in (c.get("body") or ""):
            return int(c["id"])
    return None


def run_issue_dor(
    token: str, owner: str, repo: str, issue_number: int, body: str,
    *,
    fetch_facts: IssueFactsFetcher | None = None,
    refresh_only: bool = False,
) -> dict[str, str]:
    """Evaluate one issue and post/refresh/clear Chief's advisory.

    Returns an audit dict for the dispatcher. Raises nothing on a clean
    ticket with no prior comment - that is the common path and it costs
    exactly one GitHub read.

    `refresh_only` is for the `sub_issues` event (grug#1035): a link being
    added or removed may update or clear an advisory Chief already posted,
    but must never START one. Linking an old issue to an epic is not a
    moment to lecture it about its `## Why`.
    """
    results = evaluate_issue(body, check_issue_epic(issue_number, fetch_facts))
    advisory = advisory_markdown(results)
    existing = _existing_comment(token, owner, repo, issue_number)
    if refresh_only and existing is None:
        return {"status": "no_op", "reason": "link event, no prior advisory to refresh"}
    base = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/issues/{issue_number}/comments"
    )

    if advisory is None:
        if existing is None:
            # Ready from the start: say nothing. A congratulation is a
            # notification with no information in it.
            log.info(
                "issue_dor_clean_silent",
                extra={"repo": f"{owner}/{repo}", "issue": issue_number},
            )
            return {"status": "no_op", "reason": "issue passes DoR, no prior advisory"}
        resp = httpx.patch(
            f"{base}/{existing}", json={"body": ready_markdown()},
            headers=_headers(token), timeout=_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        log.info(
            "issue_dor_resolved",
            extra={"repo": f"{owner}/{repo}", "issue": issue_number},
        )
        return {"status": "ok", "reason": "advisory cleared - issue now ready"}

    gaps = [r.name for r in results if not r.passed]
    if existing is None:
        resp = httpx.post(
            base, json={"body": advisory},
            headers=_headers(token), timeout=_FETCH_TIMEOUT,
        )
    else:
        resp = httpx.patch(
            f"{base}/{existing}", json={"body": advisory},
            headers=_headers(token), timeout=_FETCH_TIMEOUT,
        )
    resp.raise_for_status()
    log.info(
        "issue_dor_advised",
        extra={
            "repo": f"{owner}/{repo}", "issue": issue_number,
            "gaps": ",".join(gaps), "refreshed": bool(existing),
        },
    )
    return {"status": "ok", "reason": f"advisory {'refreshed' if existing else 'posted'}"}
