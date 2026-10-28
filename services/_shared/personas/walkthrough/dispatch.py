"""Teller persona - PR walkthrough dispatch (#554, epic #522, T1432).

Fetch the diff + changed-file stats, call `summarize_pr` (best-effort,
degrades to the file list alone), estimate the effort line, and publish
the walkthrough to the Hunt Board's Walkthrough section and to the
`Grug - Teller` check-run summary. Teller posts no comment of its own
(every new comment is an email); a comment an older version left on an
open pull request is not touched. Never blocks. Runs off the ACK path via
the generic async-persona machinery (#77), same as Guard/Smasher.

Lifecycle (#695): the Teller check run goes up `in_progress` BEFORE the
heavy work, and the final walkthrough PATCHes that same run. Every way out
that publishes nothing (a degrade result, a closed PR, an unexpected raise)
completes the run with a real message, so it never sits on 'working'.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx
from activity_log import record_check_verdict
from github_app_auth import with_install_token_retry
from github_checks_client import (
    CheckConclusion,
    CheckRunResult,
    patch_check_run,
    post_check_run,
)

from personas import hunt_recorder
from personas.tribe import CHECK_TELLER
from personas.walkthrough.effort import estimate_effort
from personas.walkthrough.render import FileStat, walkthrough_body

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


def _external_id(owner: str, repo: str, pull_number: int, head_sha: str) -> str:
    return f"grug-teller:{owner}/{repo}#{pull_number}:{head_sha}"


def _write_check_run(
    token: str,
    owner: str,
    repo: str,
    pull_number: int,
    result: CheckRunResult,
    run_id: int | None,
) -> int | None:
    """PATCH run `run_id` when there is one, else POST a new run. Returns the
    run id, so the in-progress run and its final content provably share one
    check run (#695)."""
    if run_id is not None:
        patch_check_run(token, owner, repo, run_id, result)
        return run_id
    created = post_check_run(
        token, owner, repo, result,
        external_id=_external_id(owner, repo, pull_number, result.head_sha),
    )
    try:
        return int(created["id"])
    except (ValueError, KeyError, TypeError):
        return None


def _start_check_run(
    installation_id: int, owner: str, repo: str, pull_number: int, head_sha: str,
) -> int | None:
    """Put the Teller run up `in_progress` before any heavy work (#695).
    Best-effort: a refused write is logged and the walk goes on without
    it; returns the run id, or None when it did not land."""
    working = CheckRunResult(
        name=CHECK_TELLER, head_sha=head_sha, status="in_progress",
        conclusion=None, title=_WORKING_TITLE, summary=_WORKING,
    )
    try:
        return with_install_token_retry(
            installation_id,
            lambda token: _write_check_run(token, owner, repo, pull_number, working, None),
        )
    except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
        log.warning(
            "walkthrough_status_failed",
            extra={"pr": f"{owner}/{repo}#{pull_number}", "phase": "start",
                   "kind": type(e).__name__},
        )
        return None


def _resolve_check_run(
    installation_id: int, owner: str, repo: str, pull_number: int,
    head_sha: str, run_id: int | None, outcome: tuple[CheckConclusion, str],
) -> None:
    """Complete an in-progress run that will get no walkthrough, so it reads
    a real message instead of 'working' forever (#695). `outcome` is
    (conclusion, message). Nothing to resolve when the start never landed;
    a refused write is logged, never raised."""
    if run_id is None:
        return
    conclusion, message = outcome
    done = CheckRunResult(
        name=CHECK_TELLER, head_sha=head_sha, status="completed",
        conclusion=conclusion, title=message, summary=message,
    )
    try:
        with_install_token_retry(
            installation_id,
            lambda token: _write_check_run(token, owner, repo, pull_number, done, run_id),
        )
    except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
        log.warning(
            "walkthrough_status_failed",
            extra={"pr": f"{owner}/{repo}#{pull_number}", "phase": "resolve",
                   "kind": type(e).__name__},
        )


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
    except Exception as e:  # noqa: BLE001 - telemetry never breaks the walkthrough
        # A silent swallow here would hide a real import/signature bug in
        # observability.emit_gauge indefinitely (FLINT) - debug, not
        # warning, since a missing gauge is never itself an operator page.
        log.debug(
            "walkthrough_emit_degraded_metric_failed",
            extra={"kind": type(e).__name__},
        )


_WORKING_TITLE = "Teller is walking the PR"
_WORKING = "Grug read the PR. Teller is drawing the map now..."
_FAILED = (
    "Teller could not finish the walkthrough this time ({why}). "
    "Grug will try again; comment `/grug improve` to nudge."
)
_OUTCOME_BY_RESULT: dict[str, tuple[CheckConclusion, str]] = {
    "fetch_failed": ("neutral", _FAILED.format(why="could not read the PR from GitHub")),
    "publish_failed": ("neutral", _FAILED.format(why="could not write the final map")),
    "skipped_closed": ("skipped", "This PR is closed, so Teller put the map down."),
}


def _config_skip(payload: dict[str, Any]) -> dict[str, str] | None:
    """The skipped_config result when the base-ref `.grug.yaml` `skip_prs`
    matches this PR (grug#1439), else None. Lazy import: Elder's dispatch
    module owns the base-ref config fetch, so both personas read the same
    file the same way."""
    from personas.code_reviewer.dispatch import pr_skip_rule

    pr = payload["pull_request"]
    owner = payload["repository"]["owner"]["login"]
    repo_name = payload["repository"]["name"]
    try:
        skip_rule = pr_skip_rule(
            pr, int(payload["installation"]["id"]), owner, repo_name,
        )
    except RuntimeError:
        # Token exchange failed; the diff fetch hits the same wall and
        # takes the established fetch_failed path.
        return None
    if skip_rule is None:
        return None
    log.info(
        "walkthrough_skipped_by_config",
        extra={"pr": f"{owner}/{repo_name}#{pr['number']}", "rule": skip_rule},
    )
    return {
        "persona": "walkthrough", "result": "skipped_config",
        "skipped_by": skip_rule,
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
    fetch or the check-run publish degrades to a logged, structured result
    (fetch_failed/publish_failed), matching Smasher's dispatch guard for
    the token-exchange RuntimeError case.

    Lifecycle (#695): the check run goes up `in_progress` BEFORE any heavy
    work, and the final content PATCHes that same run. Every way out that
    publishes nothing (a degrade result, a closed PR, an unexpected raise)
    completes the run with a real message. A PR the config skips gets no
    run at all."""
    del blocking  # advisory-only persona; kept for the shared call contract
    skipped = _config_skip(payload)
    if skipped is not None:
        return skipped
    ref = (
        int(payload["installation"]["id"]),
        payload["repository"]["owner"]["login"],
        payload["repository"]["name"],
        int(payload["pull_request"]["number"]),
        payload["pull_request"]["head"]["sha"],
    )
    run_id = _start_check_run(*ref)
    try:
        result = _walk(payload, run_id)
    except Exception as e:
        log.warning(
            "walkthrough_failed",
            extra={"pr": f"{ref[1]}/{ref[2]}#{ref[3]}", "kind": type(e).__name__},
        )
        _resolve_check_run(
            *ref, run_id,
            ("neutral", _FAILED.format(why="something broke on Grug's side")),
        )
        raise
    outcome = _OUTCOME_BY_RESULT.get(result["result"])
    if outcome is not None:
        _resolve_check_run(*ref, run_id, outcome)
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
    *,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
    head_sha: str,
) -> tuple[str | None, list[FileStat], str | None, bool]:
    """(summary, files, model_effort, degraded); the summary is None and
    `degraded` True when the LLM pass fails."""
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
    except Exception as e:  # noqa: BLE001 - a summary hiccup must not drop the walkthrough
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
        summary = None
        model_effort = None

    return summary, files, model_effort, degraded


def _walk(payload: dict[str, Any], run_id: int | None) -> dict[str, str]:
    """The walkthrough pass itself; `run_id` is the in-progress Teller run the
    final content PATCHes (None when it never landed). See
    `dispatch_walkthrough_review`."""
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

    board_body = walkthrough_body(
        summary=summary,
        files=files,
        effort=effort,
        head_sha=head_sha,
        files_truncated=files_truncated,
    )
    # The check run has no Related context list of its own; the board does.
    body = walkthrough_body(
        summary=summary,
        files=files,
        effort=effort,
        head_sha=head_sha,
        files_truncated=files_truncated,
        related=related,
    )
    title = (
        f"Teller walked {'at least ' if files_truncated else ''}"
        f"{len(files)} file(s), effort={effort}"
    )

    try:
        with_install_token_retry(
            installation_id,
            lambda token: _write_check_run(
                token, owner, repo_name, pull_number,
                CheckRunResult(
                    name=CHECK_TELLER, head_sha=head_sha, status="completed",
                    conclusion="success", title=title, summary=body,
                ),
                run_id,
            ),
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

    # conclusion stays "success" - the walkthrough WAS published; degraded_reason
    # is reserved for "Grug could not evaluate at all" (Elder/Guard's LLM
    # outage), which overstates this case (the file table still delivers real
    # value). The summary text + a best-effort gauge (same shape as
    # ticket-compliance's _emit_metric) are the honest signal for "the
    # AI-authored prose fell back" without lying the OTHER way on the
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
        text=board_body,
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
