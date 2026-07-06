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
from typing import Any
from urllib.parse import quote

import httpx

from activity_log import record_check_verdict
from github_app_auth import with_install_token_retry
from personas.walkthrough.effort import estimate_effort
from personas.walkthrough.render import MARKER, FileStat, walkthrough_body

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


def _fetch_pr_files(
    token: str, owner: str, repo: str, pull_number: int,
) -> list[FileStat]:
    """Paginated `/files` fetch -> FileStat (no summary yet - filled in
    from the LLM response, if any, after)."""
    out: list[FileStat] = []
    for page in range(1, _MAX_FILE_PAGES + 1):
        resp = httpx.get(
            f"{_API}/repos/{_repo_path(owner, repo)}/pulls/{pull_number}/files",
            params={"per_page": 100, "page": page},
            headers=_headers(token), timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        for f in batch:
            out.append(FileStat(
                path=str(f.get("filename", "")),
                additions=int(f.get("additions", 0)),
                deletions=int(f.get("deletions", 0)),
            ))
        if len(batch) < 100:
            break
    return out


def _find_marker_comment(
    token: str, owner: str, repo: str, pr_number: int,
) -> int | None:
    page = 1
    while page <= 20:  # bound the scan (>2000 comments = give up, post fresh)
        resp = httpx.get(
            f"{_API}/repos/{_repo_path(owner, repo)}/issues/{pr_number}/comments",
            params={"per_page": 100, "page": page}, headers=_headers(token), timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        for c in batch:
            if MARKER in (c.get("body") or ""):
                return int(c["id"])
        if len(batch) < 100:
            return None
        page += 1
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


def dispatch_walkthrough_review(
    payload: dict[str, Any], *, blocking: bool,
) -> dict[str, str]:
    """Entry point - one Teller walkthrough pass. `blocking` is unused
    (Teller has no blocking mode - registry requires the parameter for
    the shared async-job contract). Never raises: every failure degrades
    to a deterministic comment or a logged skip, never a wire-level
    exception."""
    del blocking  # advisory-only persona; kept for the shared call contract
    pr = payload["pull_request"]
    repo = payload["repository"]
    installation = payload["installation"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    pull_number = int(pr["number"])
    head_sha = pr["head"]["sha"]
    installation_id = int(installation["id"])

    try:
        diff_text, files = with_install_token_retry(
            installation_id,
            lambda token: (
                _fetch_pr_diff(token, owner, repo_name, pull_number),
                _fetch_pr_files(token, owner, repo_name, pull_number),
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "walkthrough_fetch_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        return {"persona": "walkthrough", "result": "fetch_failed"}

    lines_changed = sum(f.additions + f.deletions for f in files)
    degraded = False
    llm_summary = None
    try:
        from llm_client import summarize_pr  # lazy: heavy import, webhook+api both use this module

        llm_summary = summarize_pr(diff_text, [f.path for f in files], installation_id)
    except Exception as e:  # noqa: BLE001 - a summary hiccup must not drop the comment
        log.warning(
            "walkthrough_summarize_failed",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "kind": type(e).__name__},
        )

    if llm_summary is not None:
        summary = llm_summary.summary
        blurbs = llm_summary.file_summaries
        model_effort = llm_summary.effort
        files = [
            FileStat(path=f.path, additions=f.additions, deletions=f.deletions,
                     summary=blurbs.get(f.path))
            for f in files
        ]
    else:
        degraded = True
        summary = (
            f"{len(files)} file(s) changed (+{sum(f.additions for f in files)}/"
            f"-{sum(f.deletions for f in files)}). Grug's teller-voice was "
            "quiet this pass; this is the honest deterministic summary."
        )
        model_effort = None

    effort = estimate_effort(
        file_count=len(files), lines_changed=lines_changed, model_effort=model_effort,
    )

    from personas.walkthrough.mermaid import build_diagram
    diagram = build_diagram([f.path for f in files])

    body = walkthrough_body(
        summary=summary, files=files, diagram=diagram, effort=effort,
        head_sha=head_sha, degraded=degraded,
    )

    try:
        with_install_token_retry(
            installation_id,
            lambda token: _upsert_comment(token, owner, repo_name, pull_number, body),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "walkthrough_publish_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        return {"persona": "walkthrough", "result": "publish_failed"}

    record_check_verdict(
        install_id=installation_id,
        persona_key="walkthrough",
        repo=f"{owner}/{repo_name}",
        pr_number=pull_number,
        head_sha=head_sha,
        conclusion="success",
        summary=f"Teller walked {len(files)} file(s), effort={effort}",
        findings_count=0,
        blocking=False,
    )
    return {"persona": "walkthrough", "result": "degraded" if degraded else "pass"}
