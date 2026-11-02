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
from personas.tpm.sibling_evidence import delivered_criteria

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
    return files[:_MAX_FILES]  # hard cap (LORE review #535: a page could overshoot)


def _find_marker_comment(token: str, owner: str, repo: str, pr_number: int) -> int | None:
    # Paginate: on a busy PR the marker may sit past page 1, and missing it
    # would POST a duplicate instead of PATCHing (LORE review #535).
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
    # the ordinary "no marker" exit above (LORE review, PR #694), or a
    # duplicate-comment-growth bug on an extreme PR (>2000 comments) would
    # go unnoticed forever, same as walkthrough/dispatch.py already does.
    log.warning(
        "ticket_compliance_marker_scan_capped",
        extra={"repo": f"{owner}/{repo}", "pr": pr_number},
    )
    return None


def _has_existing_advisory(
    token: str, owner: str, repo: str, pr_number: int,
) -> bool:
    """Is a Chief advisory already published, in EITHER home?

    Two homes because the migration is not retroactive: PRs advised before
    #797 carry a standalone marker comment, PRs advised after carry a `chief`
    section inside the board. Checking only one of them leaves the other
    permanently stale.
    """
    try:
        from personas import board, board_client
        found = board_client.find_board(
            token, owner, repo, pr_number, app_id=get_app_id(),
        )
        if found is not None and board.extract_section(found[1], "chief"):
            return True
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        # Unknown, not absent. Falling through to the legacy check is the
        # conservative read: worst case we skip one clear-up, versus posting a
        # duplicate advisory on a PR that already has one.
        log.warning(
            "chief_board_lookup_failed",
            extra={"pr": f"{owner}/{repo}#{pr_number}", "kind": type(e).__name__},
        )
    return _find_marker_comment(token, owner, repo, pr_number) is not None


# Chief's verdict, used ONLY when Chief is the persona that creates the board.
# It never overwrites Elder's, but if Chief is first then this line is the whole
# email, and "Grug look." is not worth waking somebody for.
_CHIEF_CREATE_HEADER = "### Grug say **HOLD**. Ticket not ready for hunt."


def _upsert_comment(
    token: str, owner: str, repo: str, pr_number: int, body: str,
    *, create_if_absent: bool = True,
) -> None:
    """Write Chief's advisory as a SECTION of the shared board (#791).

    It used to be its own top-level comment, which meant its own GitHub
    notification - a second email for the same review. The board is one
    comment that every persona edits, and GitHub never mails an edit, so
    folding Chief in costs zero additional notifications.

    Chief passes no `header`: only the persona that counts findings has the
    standing to rewrite the verdict line. Chief has an opinion about the
    TICKET, not about the diff. It does pass `header_on_create`, because a
    board Chief starts is still an email and needs a readable first line.

    Falls back to its own comment if the board write fails - an advisory that
    silently vanishes is worse than one in the wrong place.
    """
    try:
        from personas import board_client
        board_client.upsert_board_section(
            token, owner, repo, pr_number,
            key="chief",
            section=body.replace(_MARKER, "").strip(),
            header_on_create=_CHIEF_CREATE_HEADER,
            create_if_absent=create_if_absent,
            # WITHOUT this, `_MARKER` is a legacy board marker, so a human who
            # quotes it in a PR discussion looks exactly like the board and
            # gets their comment overwritten. `_find_marker_comment` has always
            # checked app identity (#560); moving to the board in #797 dropped
            # the check on the way past.
            app_id=get_app_id(),
        )
        return
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "chief_board_upsert_failed_falling_back",
            extra={"pr": f"{owner}/{repo}#{pr_number}", "kind": type(e).__name__},
        )

    existing = _find_marker_comment(token, owner, repo, pr_number)
    if existing is not None:
        httpx.patch(
            f"{_API}/repos/{_repo_path(owner, repo)}/issues/comments/{existing}",
            json={"body": body}, headers=_headers(token), timeout=_TIMEOUT,
        ).raise_for_status()
    elif create_if_absent:
        httpx.post(
            f"{_API}/repos/{_repo_path(owner, repo)}/issues/{pr_number}/comments",
            json={"body": body}, headers=_headers(token), timeout=_TIMEOUT,
        ).raise_for_status()
    else:
        # The fallback has to honour the gate too. Otherwise a transient board
        # write failure turns "nothing worth mailing" into a brand new comment
        # - the one email the caller specifically declined to send, delivered
        # only when something else went wrong.
        log.info(
            "chief_create_declined_nothing_to_say",
            extra={"pr": f"{owner}/{repo}#{pr_number}"},
        )


def _less_delivered(
    token: str, owner: str, repo: str, number: int, pr_number: int,
    gaps: list[str], ticket_body: str,
) -> list[str]:
    """`gaps` less those a merged PR on the ticket delivered (grug#1530). A
    failed read leaves them: the advisory says more, never less."""
    from personas.tpm.issue_fetcher import merged_siblings

    try:
        siblings = merged_siblings(token, owner, repo, number, pr_number=pr_number)
    except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
        log.warning("ticket_compliance_siblings_failed",
                    extra={"issue": number, "err": type(e).__name__})
        return gaps
    met = delivered_criteria(gaps, siblings, ticket_body)
    return [g for g in gaps if g not in met]


def _collect_unaddressed(
    token: str, owner: str, repo: str, refs: list[int], signals: set[str],
    pr_number: int = 0,
) -> dict[int, list[str]]:
    """`{issue number: criteria that look unaddressed}` for each linked issue.

    Per-issue failures are skipped, not raised: one unreachable issue must not
    suppress the advisory for the others. A 404 body (deleted/moved issue)
    is likewise nothing to say rather than an error.
    """
    out: dict[int, list[str]] = {}
    for n in refs:
        try:
            body = _issue_body(token, owner, repo, n)
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.warning("ticket_compliance_issue_failed",
                        extra={"issue": n, "err": type(e).__name__})
            continue
        if body is None:
            continue
        gaps = unaddressed_criteria(acceptance_criteria(body), signals)
        if gaps:
            gaps = _less_delivered(token, owner, repo, n, pr_number, gaps, body)
        if gaps:
            out[n] = gaps
    return out


def _withdraw_advisory(
    token: str, owner: str, repo: str, pr_number: int, refs: list[int],
) -> bool:
    """Take down a prior advisory if one is up. True if anything was written.

    Both routes to "nothing to flag" end here - the criteria got addressed, OR
    the closing keyword went away - which is exactly why it is one function.
    Each time this logic has been open-coded at one of those two sites, the
    other one has been the bug: the board-vs-marker miss (#800) and the
    `if not refs: return` skip (#802).
    """
    if not _has_existing_advisory(token, owner, repo, pr_number):
        return False
    _upsert_comment(
        token, owner, repo, pr_number, _cleared_body(refs),
        # Clearing is only meaningful against something already posted, and a
        # board created just to announce "nothing wrong" is an email with no
        # news in it.
        create_if_absent=False,
    )
    return True


def _cleared_body(issue_numbers: list[int]) -> str:
    """The 'never mind' note that replaces a stale advisory.

    With no issue numbers the PR no longer claims to close anything, so the
    advisory has no subject left - say that plainly rather than emitting a
    dangling "addresses the acceptance criteria of ." with nothing after it.
    """
    if not issue_numbers:
        return (
            f"{_MARKER}\n"
            f"**Chief - ticket compliance.** This PR no longer claims to close "
            f"an issue, so Grug's earlier note does not apply. So speaks Grug."
        )
    refs = ", ".join(f"#{n}" for n in issue_numbers)
    return (
        f"{_MARKER}\n"
        f"**Chief - ticket compliance.** The diff now looks like it addresses "
        f"the acceptance criteria of {refs}. Advisory only. So speaks Grug."
    )


def _emit_metric(flagged: int) -> None:
    """Best-effort observability signal (LORE review #535): ticket-compliance
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
    # operator kill-switch lets it be turned off fleet-wide (LORE review #535).
    if os.getenv("GRUG_TICKET_COMPLIANCE_DISABLED", "").lower() in ("1", "true", "yes"):
        return {"checked": 0, "reason": "disabled"}
    refs = closes_refs(pr_body)[:_MAX_ISSUES]
    if not refs:
        # No closing keyword is one of the ways "nothing to flag" happens, and
        # returning here skipped the clear path entirely - so an advisory
        # posted when the body DID claim a closure was orphaned the moment the
        # author removed or corrected it, and sat there being wrong forever.
        #
        # Observed live on #801: Chief flagged a `Closes #5` it had misparsed
        # out of a code span; the example was rewritten, Chief's check went
        # green, and its section on the board still said the PR closes #5.
        out = {"checked": 0, "reason": "no closing refs"}
        if _withdraw_advisory(token, owner, repo, pr_number, []):
            out["cleared"] = True
        return out

    try:
        changed = _changed_files(token, owner, repo, pr_number)
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning("ticket_compliance_files_failed", extra={"err": type(e).__name__})
        return {"checked": 0, "reason": "files fetch failed"}
    signals = diff_signals(changed, pr_body)

    all_unaddressed = _collect_unaddressed(
        token, owner, repo, refs, signals, pr_number,
    )

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
    #
    # Must check the BOARD as well as the legacy marker. Chief's section is
    # written with `_MARKER` stripped (it would be a stray marker mid-board),
    # so once Chief moved onto the board in #797 `_find_marker_comment` could
    # never see its own advisory again and this clear path went dead: flag a
    # PR, fix the criteria, and the stale advisory sat there forever.
    out: dict = {"checked": len(refs), "flagged": {}}
    if _withdraw_advisory(token, owner, repo, pr_number, refs):
        out["cleared"] = True
    return out
