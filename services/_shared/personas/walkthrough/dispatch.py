"""Teller persona - PR walkthrough dispatch (#554, epic #522).

Fetch the diff + changed-file stats, call `summarize_pr` (best-effort,
degrades to a deterministic summary), build the effort chip and the
changed-files table, and write Teller's walkthrough into the PR's canonical
review session (the Hunt Board renders its Walkthrough section) plus the
Teller check-run summary. No advisory comment is posted: the board is the
one comment per PR, so this costs no notification and cannot clobber
another persona's section. An existing Teller comment on an open PR is left
as it is - no edit storm, no deletion. Runs off the ACK path via the
generic async-persona machinery (#77), same as Guard/Smasher.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx
from activity_log import record_check_verdict
from github_app_auth import with_install_token_retry

from personas import hunt_recorder
from personas.walkthrough.effort import estimate_effort, effort_label
from personas.walkthrough.render import FileStat, changed_files_table

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
    the token-exchange RuntimeError case."""
    del blocking  # advisory-only persona; kept for the shared call contract
    pr = payload["pull_request"]
    repo = payload["repository"]
    installation = payload["installation"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    pull_number = int(pr["number"])
    head_sha = pr["head"]["sha"]
    installation_id = int(installation["id"])
    started_at = hunt_recorder.now()

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
        return {"persona": "walkthrough", "result": "fetch_failed"}

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
        return {"persona": "walkthrough", "result": "fetch_failed"}
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

    lines_changed = sum(f.additions + f.deletions for f in files)
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
        # The deterministic fallback renders no prose paragraph at all - just
        # the file table - rather than a sentence saying the summary failed.
        summary = ""
        model_effort = None

    effort = estimate_effort(
        file_count=len(files),
        lines_changed=lines_changed,
        model_effort=model_effort,
    )

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

    # The change diagram is dropped when it has no edges: Teller's
    # deterministic diagram lists files with no edges, duplicating the
    # changed-files table below, so it is not rendered anywhere.

    # Assemble the walkthrough text for the board's Walkthrough section and
    # the Teller check-run summary from the same primitives.
    table = changed_files_table(files)
    board_parts: list[str] = []
    if summary:
        board_parts.append(summary)
    board_parts.append(f"Review effort: {effort_label(effort)}")
    if table:
        board_parts.append("")
        board_parts.append(table)
    board_text = "\n".join(board_parts)

    check_parts: list[str] = [f"Review effort: {effort_label(effort)}"]
    if table:
        check_parts.append("")
        check_parts.append(table)
    check_summary = "\n".join(check_parts)

    # Hunt Board (#1207): the walkthrough and related hunts live in the PR's
    # canonical review session; the Hunt Board renders its Walkthrough
    # section from session.walkthrough. No advisory comment is posted.
    hunt_recorder.record_walkthrough(
        hunt_recorder.PrRef(installation_id, owner, repo_name, pull_number),
        head_sha=head_sha,
        trigger=str(payload.get("action", "")),
        started_at=started_at,
        backend="",
        model="deterministic-fallback" if degraded else "",
        text=board_text,
        related=tuple(f"#{r.number} {r.title} ({r.reason})" for r in related),
    )
    record_check_verdict(
        install_id=installation_id,
        persona_key="walkthrough",
        repo=f"{owner}/{repo_name}",
        pr_number=pull_number,
        head_sha=head_sha,
        conclusion="success",
        summary=check_summary,
        findings_count=0,
        blocking=False,
    )
    _emit_degraded_metric(degraded)
    return {"persona": "walkthrough", "result": "degraded" if degraded else "pass"}
