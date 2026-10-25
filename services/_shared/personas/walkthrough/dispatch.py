"""Teller persona - PR walkthrough dispatch (#554, epic #522).

Fetch the diff + changed-file stats, call `summarize_pr` (best-effort,
degrades to a deterministic summary), build the deterministic mermaid
diagram + effort chip, and upsert ONE advisory comment (find-marker ->
PATCH, else POST) - the same discipline as Chief's ticket-compliance.
Comment-only: no check-run, never blocks. Runs off the ACK path via the
generic async-persona machinery (#77), same as Guard/Smasher.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx
from activity_log import record_check_verdict
from github_app_auth import get_app_id, with_install_token_retry

from personas import hunt_recorder
from personas.walkthrough.effort import estimate_effort
from personas.walkthrough.render import (
    MARKER,
    FileStat,
    PreviousRun,
    previous_run,
    walkthrough_body,
    with_status,
)

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.walkthrough")

_API = "https://api.github.com"
_TIMEOUT = 10.0
_MAX_FILE_PAGES = 5  # 5 x 100 = 500 files max - a bound, not a guess


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo_path(owner: str, repo: str) -> str:
    return f"{quote(owner, safe='')}/{quote(repo, safe='')}"


def _fetch_pr_diff(token: str, owner: str, repo: str, pull_number: int) -> str:
    resp = httpx.get(
        f"{_API}/repos/{_repo_path(owner, repo)}/pulls/{pull_number}",
        headers={**_headers(token), "Accept": "application/vnd.github.diff"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def _pr_is_closed(token: str, owner: str, repo: str, pull_number: int) -> bool:
    """Whether the pull request is closed (merged or not) now. A failed read
    says False, so the walkthrough still posts as before."""
    try:
        resp = httpx.get(
            f"{_API}/repos/{_repo_path(owner, repo)}/pulls/{pull_number}",
            headers=_headers(token),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("state") == "closed"
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def _fetch_pr_files(
    token: str,
    owner: str,
    repo: str,
    pull_number: int,
) -> tuple[list[FileStat], bool]:
    """Paginated `/files` fetch -> (FileStat list, truncated). GitHub's own
    endpoint caps at 3000 files - well above our _MAX_FILE_PAGES bound - so
    a monorepo migration or generated-file dump can legitimately exceed our
    cap. `truncated=True` when the cap was hit with a still-full last page,
    so the caller can say so honestly instead of presenting a truncated
    count as exact (no summary yet - filled in from the LLM response, if
    any, after)."""
    out: list[FileStat] = []
    truncated = False
    for page in range(1, _MAX_FILE_PAGES + 1):
        resp = httpx.get(
            f"{_API}/repos/{_repo_path(owner, repo)}/pulls/{pull_number}/files",
            params={"per_page": 100, "page": page},
            headers=_headers(token),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        out.extend(
            FileStat(
                path=str(f.get("filename", "")),
                additions=int(f.get("additions", 0)),
                deletions=int(f.get("deletions", 0)),
            )
            for f in batch
        )
        if len(batch) < 100:
            break
        if page == _MAX_FILE_PAGES:
            truncated = True
    return out, truncated


def _find_marker_comment(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
) -> dict[str, Any] | None:
    # `performed_via_github_app` is populated server-side ONLY for comments
    # created via a GitHub App installation token - a human contributor
    # cannot set it. But a bare non-null check only proves "some GitHub
    # App posted this," not "WE did" - a decoy from a DIFFERENT installed
    # app would still pass. Compare the app's own numeric ID (#554 peer
    # review round 3, codex). `get_app_id()` is guaranteed to already have
    # succeeded once by this point - it backs the very token exchange
    # that produced `token` - so no fallback-on-failure path exists here.
    own_app_id = get_app_id()
    page = 1
    while (
        page <= 20
    ):  # bound the scan (>2000 comments = give up, post fresh, warn below)
        resp = httpx.get(
            f"{_API}/repos/{_repo_path(owner, repo)}/issues/{pr_number}/comments",
            params={"per_page": 100, "page": page},
            headers=_headers(token),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        for c in batch:
            app = c.get("performed_via_github_app")
            if not app or str(app.get("id")) != own_app_id:
                continue
            if MARKER in (c.get("body") or ""):
                return c
        if len(batch) < 100:
            return None
        page += 1
    # Gave up at the cap without finding the marker - distinguish this from
    # the ordinary "no marker" exit above, or a duplicate-comment-growth
    # bug on an extreme PR (>2000 comments) would go unnoticed forever.
    log.warning(
        "walkthrough_marker_scan_capped",
        extra={"repo": f"{owner}/{repo}", "pr": pr_number},
    )
    return None


def _upsert_comment(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    make_body: Callable[[dict[str, Any] | None], str],
) -> int | None:
    """Find-by-marker -> PATCH, else POST. `make_body` gets the existing
    comment (None on the first run) so the new body can build on it. Returns
    the comment id, so a placeholder and its final content provably share
    one comment (#695)."""
    existing = _find_marker_comment(token, owner, repo, pr_number)
    if existing is not None:
        httpx.patch(
            f"{_API}/repos/{_repo_path(owner, repo)}/issues/comments/{existing['id']}",
            json={"body": make_body(existing)},
            headers=_headers(token),
            timeout=_TIMEOUT,
        ).raise_for_status()
        return int(existing["id"])
    resp = httpx.post(
        f"{_API}/repos/{_repo_path(owner, repo)}/issues/{pr_number}/comments",
        json={"body": make_body(None)},
        headers=_headers(token),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    try:
        return int(resp.json()["id"])
    except (ValueError, KeyError, TypeError):
        return None


def _existing_previous(existing: dict[str, Any] | None) -> PreviousRun | None:
    return previous_run(existing.get("body") if existing else None)


def _set_status(
    installation_id: int, owner: str, repo: str, pr_number: int, text: str,
) -> bool:
    """Say what Teller is doing in the PR's one walkthrough comment (#695).

    The status line goes on top of whatever the comment already holds, so a
    re-run shows 'working' over the last walkthrough instead of erasing it.
    Best-effort: a refused write is logged and the run goes on; True when
    the status landed."""
    try:
        with_install_token_retry(
            installation_id,
            lambda token: _upsert_comment(
                token, owner, repo, pr_number,
                lambda existing: with_status(
                    existing.get("body") if existing else None, text,
                ),
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
        log.warning(
            "walkthrough_status_failed",
            extra={
                "pr": f"{owner}/{repo}#{pr_number}",
                "kind": type(e).__name__,
            },
        )
        return False
    return True


def _log_fetch_failed(
    e: Exception,
    *,
    phase: str,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
) -> None:
    """Log a fetch failure with the PHASE it happened in + the real status
    code on an HTTPStatusError - "HTTPStatusError" alone can't distinguish
    a full auth outage from a page-3 rate limit, and both look identical
    without this."""
    extra: dict[str, Any] = {
        "installation_id": installation_id,
        "pr": f"{owner}/{repo_name}#{pull_number}",
        "phase": phase,
        "kind": type(e).__name__,
    }
    if isinstance(e, httpx.HTTPStatusError):
        extra["status_code"] = e.response.status_code
    log.warning("walkthrough_fetch_failed", extra=extra)


def _self_recover(
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
) -> None:
    """Enqueue ONE durable rerun on the SAME rerun lane Elder/Guard/Smasher
    use for an unhandled dispatch error (#554 peer review, CONFIRMED 3x -
    codex/poolside/spark independently). Teller's dispatch never raises
    (matching the sibling contract - see this function's docstring), so
    `_run_job`'s claim-then-dispatch ordering would otherwise consume the
    head-SHA claim on a transient fetch/publish failure with NOTHING
    published and no retry ever firing - permanently suppressing the
    walkthrough for that SHA. `rerun` is webhook-only (this shared module
    is also importable by the api service, where the rerun consumer never
    runs), so the import stays inside the function - same pattern as
    webhook_dispatch.py's async_dispatch import. Best-effort: an enqueue
    failure here must not turn a degrade path into a raise."""
    try:
        from rerun import enqueue_rerun  # lazy: webhook-only

        enqueue_rerun(
            install_id=installation_id,
            repo=f"{owner}/{repo_name}",
            pr_number=pull_number,
            persona="walkthrough",
        )
    except Exception as e:  # noqa: BLE001 - best-effort, never escalate a degrade
        log.warning(
            "walkthrough_self_recover_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )


def _emit_degraded_metric(degraded: bool) -> None:
    """Best-effort observability signal: repeated LLM-summary degradation
    is invisible on the Activity feed (conclusion stays "success" - see
    the call site), so a DD monitor needs its own gauge to catch it."""
    try:
        from observability import emit_gauge  # type: ignore

        emit_gauge("grug.teller.summary_degraded", 1 if degraded else 0)
    except Exception as e:  # noqa: BLE001 - telemetry never breaks the comment
        # A silent swallow here would hide a real import/signature bug in
        # observability.emit_gauge indefinitely (FLINT) - debug, not
        # warning, since a missing gauge is never itself an operator page.
        log.debug(
            "walkthrough_emit_degraded_metric_failed",
            extra={"kind": type(e).__name__},
        )


_WORKING = "Grug read the PR. Teller is drawing the map now..."
_FAILED = (
    "Teller could not finish the walkthrough this time ({why}). "
    "Grug will try again; comment `/grug improve` to nudge."
)
_STATUS_BY_RESULT = {
    "fetch_failed": _FAILED.format(why="could not read the PR from GitHub"),
    "publish_failed": _FAILED.format(why="could not write the final map"),
    "skipped_closed": "This PR is closed, so Teller put the map down.",
}


def dispatch_walkthrough_review(
    payload: dict[str, Any],
    *,
    blocking: bool,
) -> dict[str, str]:
    """Entry point - one Teller walkthrough pass. `blocking` is unused
    (Teller has no blocking mode - registry requires the parameter for
    the shared async-job contract). Never raises on the guarded surfaces:
    every HTTPStatusError/RequestError/RuntimeError from the diff/files
    fetch or the comment publish degrades to a logged, structured result
    (fetch_failed/publish_failed), matching Smasher's dispatch guard for
    the token-exchange RuntimeError case.

    Lifecycle (#695): a status line goes into the PR's walkthrough comment
    BEFORE any heavy work, and the final content PATCHes that same comment.
    Every way out that publishes nothing (a degrade result, a closed PR, an
    unexpected raise) resolves the status to a real message, so the comment
    is never left on 'still working'."""
    del blocking  # advisory-only persona; kept for the shared call contract
    pr_ref = (
        int(payload["installation"]["id"]),
        payload["repository"]["owner"]["login"],
        payload["repository"]["name"],
        int(payload["pull_request"]["number"]),
    )
    _set_status(*pr_ref, _WORKING)
    try:
        result = _walk(payload)
    except Exception:
        _set_status(*pr_ref, _FAILED.format(why="something broke on Grug's side"))
        raise
    status = _STATUS_BY_RESULT.get(result["result"])
    if status is not None:
        _set_status(*pr_ref, status)
    return result


def _fetch_inputs(
    installation_id: int, owner: str, repo_name: str, pull_number: int
) -> tuple[str, list[FileStat], bool] | None:
    """The diff and file list, or None after logging and self-recovering."""
    try:
        diff_text = with_install_token_retry(
            installation_id,
            lambda token: _fetch_pr_diff(token, owner, repo_name, pull_number),
        )
    except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
        _log_fetch_failed(
            e,
            phase="diff",
            installation_id=installation_id,
            owner=owner,
            repo_name=repo_name,
            pull_number=pull_number,
        )
        _self_recover(installation_id, owner, repo_name, pull_number)
        return None

    try:
        files, files_truncated = with_install_token_retry(
            installation_id,
            lambda token: _fetch_pr_files(token, owner, repo_name, pull_number),
        )
    except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
        # The diff fetch above already succeeded and is discarded here -
        # acceptable (the whole walkthrough needs both), but the log must
        # say WHICH call failed so a diff-outage and a files-outage don't
        # look identical.
        _log_fetch_failed(
            e,
            phase="files",
            installation_id=installation_id,
            owner=owner,
            repo_name=repo_name,
            pull_number=pull_number,
        )
        _self_recover(installation_id, owner, repo_name, pull_number)
        return None
    if files_truncated:
        # GitHub's own /files cap is 3000 - well above our _MAX_FILE_PAGES
        # bound - so a monorepo migration or generated-file dump can
        # legitimately exceed it. Every downstream number (line count,
        # effort estimate, comment text) is computed off the truncated
        # list; say so, never present it as exact.
        log.warning(
            "walkthrough_file_fetch_capped",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "files_seen": len(files),
            },
        )

    return diff_text, files, files_truncated


def _summarize_walk(
    diff_text: str,
    files: list[FileStat],
    files_truncated: bool,
    *,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
    head_sha: str,
) -> tuple[str, list[FileStat], str | None, bool]:
    """(summary, files, model_effort, degraded); falls back to a deterministic
    summary when the LLM pass fails."""
    degraded = False
    llm_summary = None
    try:
        from llm_client import (
            summarize_pr,  # lazy: heavy import, webhook+api both use this module
        )

        llm_summary = summarize_pr(
            diff_text,
            [f.path for f in files],
            installation_id,
            pr_context={
                "installation_id": installation_id,
                "repo": f"{owner}/{repo_name}",
                "pr_number": pull_number,
                "head_sha": head_sha,
            },
        )
    except Exception as e:  # noqa: BLE001 - a summary hiccup must not drop the comment
        log.warning(
            "walkthrough_summarize_failed",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )

    if llm_summary is not None:
        summary = llm_summary.summary
        blurbs = llm_summary.file_summaries
        model_effort = llm_summary.effort
        files = [
            FileStat(
                path=f.path,
                additions=f.additions,
                deletions=f.deletions,
                summary=blurbs.get(f.path),
            )
            for f in files
        ]
    else:
        degraded = True
        count_hedge = "at least " if files_truncated else ""
        summary = (
            f"{count_hedge}{len(files)} file(s) changed "
            f"(+{sum(f.additions for f in files)}/-{sum(f.deletions for f in files)}). "
            "Grug's teller-voice was quiet this pass; this is the honest "
            "deterministic summary."
        )
        model_effort = None

    return summary, files, model_effort, degraded


def _walk(payload: dict[str, Any]) -> dict[str, str]:
    """The walkthrough pass itself; see `dispatch_walkthrough_review`."""
    pr = payload["pull_request"]
    repo = payload["repository"]
    installation = payload["installation"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    pull_number = int(pr["number"])
    head_sha = pr["head"]["sha"]
    installation_id = int(installation["id"])
    started_at = hunt_recorder.now()

    fetched = _fetch_inputs(installation_id, owner, repo_name, pull_number)
    if fetched is None:
        return {"persona": "walkthrough", "result": "fetch_failed"}
    diff_text, files, files_truncated = fetched

    lines_changed = sum(f.additions + f.deletions for f in files)
    summary, files, model_effort, degraded = _summarize_walk(
        diff_text,
        files,
        files_truncated,
        installation_id=installation_id,
        owner=owner,
        repo_name=repo_name,
        pull_number=pull_number,
        head_sha=head_sha,
    )

    effort = estimate_effort(
        file_count=len(files),
        lines_changed=lines_changed,
        model_effort=model_effort,
    )

    from personas.walkthrough.mermaid import build_diagram

    diagram = build_diagram([f.path for f in files])

    # #675: possibly-related prior hunts. Best-effort and bounded (one list
    # call + at most 5 file fetches, all inside one 12s budget); a failure
    # just omits the section.
    related = []
    try:
        from personas.walkthrough.related import find_related_prs

        related = with_install_token_retry(
            installation_id,
            lambda token: find_related_prs(
                token,
                owner,
                repo_name,
                pull_number,
                [f.path for f in files],
                pr.get("title", ""),
            ),
        )
    except Exception as e:  # noqa: BLE001 - related hunts are enrichment
        log.info(
            "walkthrough_related_failed",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )

    # A walkthrough that lands after the merge is one more email with nothing
    # left to review: a lane pull request got one 6.5 minutes after it merged
    # (2026-10-18). Checked here, after the slow summary, not at the start.
    try:
        closed = with_install_token_retry(
            installation_id,
            lambda token: _pr_is_closed(token, owner, repo_name, pull_number),
        )
    except RuntimeError:
        closed = False
    if closed is True:
        log.info(
            "walkthrough_skipped_closed",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}"},
        )
        return {"persona": "walkthrough", "result": "skipped_closed"}

    # The full review is Elder's check run output on the Checks tab: the
    # walkthrough is a comment on the PR page, so linking that page only
    # reloaded it.
    pr_url = f"https://github.com/{owner}/{repo_name}/pull/{pull_number}/checks"

    def build_body(existing: dict[str, Any] | None) -> str:
        return walkthrough_body(
            summary=summary,
            files=files,
            diagram=diagram,
            effort=effort,
            head_sha=head_sha,
            degraded=degraded,
            files_truncated=files_truncated,
            related=related,
            pr_url=pr_url,
            previous=_existing_previous(existing),
        )

    try:
        with_install_token_retry(
            installation_id,
            lambda token: _upsert_comment(token, owner, repo_name, pull_number, build_body),
        )
    except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
        log.warning(
            "walkthrough_publish_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        _self_recover(installation_id, owner, repo_name, pull_number)
        return {"persona": "walkthrough", "result": "publish_failed"}

    # conclusion stays "success" - the comment WAS published; degraded_reason
    # is reserved for "Grug could not evaluate at all" (Elder/Guard's LLM
    # outage), which overstates this case (the deterministic fallback still
    # delivers real value). The summary text + a best-effort gauge (same
    # shape as ticket-compliance's _emit_metric) are the honest signal for
    # "the AI-authored prose fell back" without lying the OTHER way on the
    # Activity badge.
    _emit_degraded_metric(degraded)
    # Hunt Board (#1207): the walkthrough and related hunts live in the PR's
    # canonical review session. The summary call reports no model, so a
    # fallback summary is named as such rather than leaving a blank.
    hunt_recorder.record_walkthrough(
        hunt_recorder.PrRef(installation_id, owner, repo_name, pull_number),
        head_sha=head_sha,
        trigger=str(payload.get("action", "")),
        started_at=started_at,
        backend="",
        model="deterministic-fallback" if degraded else "",
        text=summary,
        related=tuple(f"#{r.number} {r.title} ({r.reason})" for r in related),
    )
    record_check_verdict(
        install_id=installation_id,
        persona_key="walkthrough",
        repo=f"{owner}/{repo_name}",
        pr_number=pull_number,
        head_sha=head_sha,
        conclusion="success",
        summary=(
            f"Teller walked {'at least ' if files_truncated else ''}"
            f"{len(files)} file(s), effort={effort}"
            + (" (LLM summary degraded to fallback)" if degraded else "")
        ),
        findings_count=0,
        blocking=False,
    )
    return {"persona": "walkthrough", "result": "degraded" if degraded else "pass"}
