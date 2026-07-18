"""Chief ticket-compliance - impure runner (#529).

Thin I/O around the pure heuristic in `ticket_compliance`: fetch the
linked issue body + the PR's changed files, decide (pure), then UPSERT a
single advisory comment (find-by-marker -> PATCH, else POST) so repeated
webhook deliveries never spam the PR. Advisory only - it posts a comment,
never a check-run, and never gates the merge.

Runs best-effort from the Chief dispatch: any failure is logged and
swallowed so a compliance hiccup can't starve the other personas or the
DoR verdict.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import httpx  # type: ignore

from github_app_auth import get_app_id
from personas.tpm.ticket_compliance import (
    acceptance_criteria,
    advisory_markdown,
    closes_refs,
    diff_signals,
    unaddressed_criteria,
)

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.tpm.ticket_compliance")

_API = "https://api.github.com"
_TIMEOUT = 10.0
_MARKER = "<!-- grug-chief:ticket-compliance -->"
# Cap the criteria we cross-check so a pathological issue can't fan out
# into dozens of API calls / a giant comment.
_MAX_ISSUES = 5
_MAX_FILES = 300


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo_path(owner: str, repo: str) -> str:
    return f"{quote(owner, safe='')}/{quote(repo, safe='')}"


def _issue_body(token: str, owner: str, repo: str, number: int) -> str | None:
    resp = httpx.get(
        f"{_API}/repos/{_repo_path(owner, repo)}/issues/{number}",
        headers=_headers(token), timeout=_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("body") or ""


def _changed_files(token: str, owner: str, repo: str, pr_number: int) -> list[str]:
    files: list[str] = []
    page = 1
    while len(files) < _MAX_FILES:
        resp = httpx.get(
            f"{_API}/repos/{_repo_path(owner, repo)}/pulls/{pr_number}/files",
            params={"per_page": 100, "page": page},
            headers=_headers(token), timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        files.extend(f["filename"] for f in batch)
        if len(batch) < 100:
            break
        page += 1
    return files[:_MAX_FILES]  # hard cap (Qodo review #535: a page could overshoot)


def _find_marker_comment(token: str, owner: str, repo: str, pr_number: int) -> int | None:
    # Paginate: on a busy PR the marker may sit past page 1, and missing it
    # would POST a duplicate instead of PATCHing (Qodo review #535).
    #
    # `performed_via_github_app` is populated server-side ONLY for comments
    # created via a GitHub App installation token - a human contributor
    # posting the literal marker string ahead of Chief's own run would
    # otherwise make this match a decoy comment the app can't PATCH,
    # 403/404ing every subsequent dispatch until manually removed (#560,
    # same class as Teller's #554 round-3 fix). A bare non-null check only
    # proves "some GitHub App posted this," not "WE did" - compare the
    # app's own numeric ID to rule out a decoy from a DIFFERENT installed
    # app too. `get_app_id()` is guaranteed to already have succeeded once
    # by this point - it backs the very token exchange that produced
    # `token` - so no fallback-on-failure path exists here.
    own_app_id = get_app_id()
    page = 1
    while page <= 20:  # bound the scan (>2000 comments = give up, post fresh, warn below)
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
            if _MARKER in (c.get("body") or ""):
                return int(c["id"])
        if len(batch) < 100:
            return None
        page += 1
    # Gave up at the cap without finding the marker - distinguish this from
    # the ordinary "no marker" exit above (Qodo review, PR #694), or a
    # duplicate-comment-growth bug on an extreme PR (>2000 comments) would
    # go unnoticed forever, same as walkthrough/dispatch.py already does.
    log.warning(
        "ticket_compliance_marker_scan_capped",
        extra={"repo": f"{owner}/{repo}", "pr": pr_number},
    )
    return None


def _upsert_comment(
    token: str, owner: str, repo: str, pr_number: int, body: str,
) -> None:
    existing = _find_marker_comment(token, owner, repo, pr_number)
    if existing is not None:
        httpx.patch(
            f"{_API}/repos/{_repo_path(owner, repo)}/issues/comments/{existing}",
            json={"body": body}, headers=_headers(token), timeout=_TIMEOUT,
        ).raise_for_status()
    else:
        httpx.post(
            f"{_API}/repos/{_repo_path(owner, repo)}/issues/{pr_number}/comments",
            json={"body": body}, headers=_headers(token), timeout=_TIMEOUT,
        ).raise_for_status()


def _cleared_body(issue_numbers: list[int]) -> str:
    refs = ", ".join(f"#{n}" for n in issue_numbers)
    return (
        f"{_MARKER}\n"
        f"**Chief - ticket compliance.** The diff now looks like it addresses "
        f"the acceptance criteria of {refs}. Advisory only. So speaks Grug."
    )


def _emit_metric(flagged: int) -> None:
    """Best-effort observability signal (Qodo review #535): ticket-compliance
    posts an advisory COMMENT (not a check-run), so a check-verdict row would
    misrepresent it - emit an owned gauge instead."""
    try:
        from observability import emit_gauge  # type: ignore
        emit_gauge("grug.chief.ticket_compliance.flagged", flagged)
    except Exception:  # noqa: BLE001 - telemetry never breaks the advisory
        pass


def run_ticket_compliance(
    token: str, *, owner: str, repo: str, pr_number: int, pr_body: str,
) -> dict[str, object]:
    """Cross-check the PR diff against the acceptance criteria of each issue
    it claims to close; upsert one advisory comment. Returns a small result
    dict for logging. Best-effort: raises only on a programming error, not
    on GitHub hiccups (the dispatch also guards)."""
    # Toggle: per-repo it inherits Chief's tpm_enabled (this runs only inside
    # the Chief dispatch, which the registry gates per repo). A global
    # operator kill-switch lets it be turned off fleet-wide (Qodo review #535).
    if os.getenv("GRUG_TICKET_COMPLIANCE_DISABLED", "").lower() in ("1", "true", "yes"):
        return {"checked": 0, "reason": "disabled"}
    refs = closes_refs(pr_body)[:_MAX_ISSUES]
    if not refs:
        return {"checked": 0, "reason": "no closing refs"}

    try:
        changed = _changed_files(token, owner, repo, pr_number)
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning("ticket_compliance_files_failed", extra={"err": type(e).__name__})
        return {"checked": 0, "reason": "files fetch failed"}
    signals = diff_signals(changed, pr_body)

    all_unaddressed: dict[int, list[str]] = {}
    for n in refs:
        try:
            body = _issue_body(token, owner, repo, n)
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.warning("ticket_compliance_issue_failed", extra={"issue": n, "err": type(e).__name__})
            continue
        if body is None:
            continue
        gaps = unaddressed_criteria(acceptance_criteria(body), signals)
        if gaps:
            all_unaddressed[n] = gaps

    # One comment for the whole PR: concatenate per-issue advisories, or a
    # cleared note when a prior advisory is now satisfied.
    sections = [advisory_markdown(n, gaps) for n, gaps in all_unaddressed.items()]
    sections = [s for s in sections if s]
    total_flagged = sum(len(g) for g in all_unaddressed.values())
    _emit_metric(total_flagged)
    if sections:
        # Strip the duplicate marker/preamble from all but the first block.
        body = sections[0]
        for extra in sections[1:]:
            body += "\n\n" + extra.split("\n", 1)[1]  # drop the leading marker line
        _upsert_comment(token, owner, repo, pr_number, body)
        return {"checked": len(refs), "flagged": {n: len(g) for n, g in all_unaddressed.items()}}

    # Nothing unaddressed. If a stale advisory exists, clear it; else no-op.
    if _find_marker_comment(token, owner, repo, pr_number) is not None:
        _upsert_comment(token, owner, repo, pr_number, _cleared_body(refs))
        return {"checked": len(refs), "flagged": {}, "cleared": True}
    return {"checked": len(refs), "flagged": {}}
