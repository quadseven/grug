"""`/grug fix`: apply all unresolved committable suggestions as one bot commit.

Collects open findings from the PR's review session that carry a suggestion,
verifies each still applies cleanly against the current head, applies the
valid ones as a single commit attributed to the bot, and reports what was
applied and what was skipped.

Suggestions that no longer apply (the anchored line changed, or the fix no
longer parses) are listed as skipped and never force-applied.

All side effects (store reads, GitHub API calls, the commit itself) flow
through injected seams so the logic is unit-testable without a network or a
database.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote as _q

from personas.code_reviewer.suggestion import checked_suggestion
from review_session import FindingRecord

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.fix")

_API = "https://api.github.com"
_TIMEOUT = 15.0
_BOT_NAME = "grug-tribe[bot]"
_BOT_EMAIL = "grug-tribe[bot]@users.noreply.github.com"


class _FixCandidate:
    """Minimal Finding-shaped view of a FindingRecord for checked_suggestion."""

    __slots__ = ("file", "line", "suggestion", "message")

    def __init__(self, rec: FindingRecord) -> None:
        self.file = rec.path
        self.line = rec.line
        self.suggestion = rec.suggestion
        self.message = rec.message


def _open_committable_findings(
    session: Any,
) -> list[FindingRecord]:
    """Open findings from the session that carry a suggestion."""
    return [
        f for f in session.findings
        if f.state == "open" and f.suggestion
    ]


def _fix_still_applies(
    rec: FindingRecord, file_text: str,
) -> str | None:
    """Return the verified replacement line, or None when it no longer applies."""
    return checked_suggestion(_FixCandidate(rec), rec.line, file_text)


def _default_post_reply(
    token_fn, owner: str, repo: str, pr_number: int, body: str,
) -> bool:
    import httpx  # type: ignore

    try:
        token_fn(lambda t: httpx.post(
            f"{_API}/repos/{_q(owner, safe='')}/{_q(repo, safe='')}"
            f"/issues/{int(pr_number)}/comments",
            json={"body": body},
            headers={
                "Authorization": f"token {t}",
                "Accept": "application/vnd.github+json",
            },
            timeout=_TIMEOUT,
        ).raise_for_status())
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning("fix_reply_failed", extra={"kind": type(e).__name__})
        return False
    return True


def _default_get_session(
    install_id: int, repo: str, pr_number: int,
) -> Any | None:
    from adapters.pg_install_store import get_review_session  # type: ignore
    return get_review_session(
        install_id=install_id, repo=repo, pr_number=int(pr_number),
    )


def _default_fetch_file(
    token_fn, owner: str, repo: str, path: str, ref: str,
) -> str | None:
    import httpx  # type: ignore
    from urllib.parse import quote as _q

    try:
        def _fetch(token: str) -> str:
            response = httpx.get(
                f"{_API}/repos/{_q(owner, safe='')}/{_q(repo, safe='')}"
                f"/contents/{_q(path, safe='/')}",
                params={"ref": ref},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.raw",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
            return response.text

        return token_fn(_fetch)
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.info("fix_file_fetch_skipped", extra={"path": path, "kind": type(e).__name__})
        return None


def _default_apply_commit(
    token_fn, owner: str, repo: str, head_sha: str,
    changes: dict[str, str], message: str, pr_number: int,
) -> str | None:
    """Create one commit on the PR branch applying `changes` (path -> new text).

    Returns the new head SHA, or None when the commit could not be created.
    Uses the GitHub Git Data API: blobs -> tree -> commit -> update ref.
    """
    import httpx  # type: ignore

    def _req(token: str, method: str, url: str, json: Any = None) -> httpx.Response:
        h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if method == "GET":
            return httpx.get(url, headers=h, timeout=_TIMEOUT)
        if method == "PATCH":
            return httpx.patch(url, headers=h, json=json, timeout=_TIMEOUT)
        return httpx.post(url, headers=h, json=json, timeout=_TIMEOUT)

    try:
        return token_fn(lambda t: _do_commit(
            t, owner, repo, head_sha, changes, message, pr_number, _req,
        ))
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning("fix_commit_failed", extra={"kind": type(e).__name__})
        return None


def _do_commit(
    token: str, owner: str, repo: str, head_sha: str,
    changes: dict[str, str], message: str, pr_number: int, _req,
) -> str:
    """Actual GitHub Git Data API sequence. Raises on failure."""
    import base64
    from urllib.parse import quote
    base_url = f"{_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    # Learn the PR's head branch name so we can update the ref.
    pr_resp = _req(token, "GET", f"{base_url}/pulls/{int(pr_number)}")
    pr_resp.raise_for_status()
    head_ref = pr_resp.json()["head"]["ref"]

    # Get the head commit's tree.
    commit_resp = _req(token, "GET", f"{base_url}/git/commits/{head_sha}")
    commit_resp.raise_for_status()
    base_tree_sha = commit_resp.json()["tree"]["sha"]

    # Create blobs for every changed file.
    blob_shas: dict[str, str] = {}
    for path, text in changes.items():
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        blob_resp = _req(token, "POST", f"{base_url}/git/blobs",
                         json={"content": encoded, "encoding": "base64"})
        blob_resp.raise_for_status()
        blob_shas[path] = blob_resp.json()["sha"]

    # Build a new tree on top of the base tree with the changed files swapped.
    tree_entries = [
        {"path": path, "mode": "100644", "type": "blob", "sha": blob_shas[path]}
        for path in changes
    ]
    tree_resp = _req(token, "POST", f"{base_url}/git/trees",
                     json={"base_tree": base_tree_sha, "tree": tree_entries})
    tree_resp.raise_for_status()
    new_tree_sha = tree_resp.json()["sha"]

    # Create the commit, attributed to the bot.
    commit_resp = _req(token, "POST", f"{base_url}/git/commits",
                       json={
                           "message": message,
                           "tree": new_tree_sha,
                           "parents": [head_sha],
                           "author": {"name": _BOT_NAME, "email": _BOT_EMAIL},
                           "committer": {"name": _BOT_NAME, "email": _BOT_EMAIL},
                       })
    commit_resp.raise_for_status()
    new_commit_sha = commit_resp.json()["sha"]

    # Update the branch ref to the new commit.
    ref_resp = _req(
        token, "PATCH",
        f"{base_url}/git/ref/heads/{quote(head_ref, safe='')}",
        json={"sha": new_commit_sha, "force": False},
    )
    ref_resp.raise_for_status()
    return new_commit_sha


def _prepare_changes(
    candidates: list[FindingRecord], *, token_fn, owner: str, repo: str,
    head_sha: str, fetch_file,
) -> tuple[list[FindingRecord], list[tuple[FindingRecord, str]], dict[str, str]]:
    """Verify candidates and group their safe replacements by file."""
    applied: list[FindingRecord] = []
    skipped: list[tuple[FindingRecord, str]] = []
    file_lines: dict[str, list[str]] = {}
    trailing_newline: dict[str, bool] = {}
    patched_lines: set[tuple[str, int]] = set()

    for rec in candidates:
        file_text = fetch_file(token_fn, owner, repo, rec.path, head_sha)
        if file_text is None:
            skipped.append((rec, "could not read the file at this head"))
            continue
        file_lines.setdefault(rec.path, file_text.splitlines())
        trailing_newline[rec.path] = file_text.endswith("\n")
        replacement = _fix_still_applies(rec, file_text)
        if replacement is None:
            skipped.append((rec, "suggestion no longer applies cleanly"))
            continue
        location = (rec.path, rec.line)
        if location in patched_lines:
            skipped.append((rec, "another suggestion targets the same line"))
            continue
        file_lines[rec.path][rec.line - 1] = replacement
        patched_lines.add(location)
        applied.append(rec)

    changes = {
        path: "\n".join(lines) + ("\n" if trailing_newline[path] else "")
        for path, lines in file_lines.items()
        if any(f.path == path for f in applied)
    }
    return applied, skipped, changes


def _load_candidates(
    *, install_id: int, repo: str, pr_number: int, get_session, reply,
) -> tuple[Any | None, list[FindingRecord], dict[str, Any] | None]:
    """Load the review session and handle requests with no fixable findings."""
    session = get_session(install_id, repo, pr_number)
    if not session:
        reply("Grug found no review session for this PR, so there is nothing to fix.")
        return None, [], {"verb": "fix", "status": "skip", "reason": "no_review_session"}

    candidates = _open_committable_findings(session)
    if candidates:
        return session, candidates, None

    reply("Grug found no unresolved committable suggestions on this PR.")
    return session, [], {"verb": "fix", "status": "no_op", "reason": "no_committable_findings"}


def _commit_and_report(
    *, applied: list[FindingRecord], skipped: list[tuple[FindingRecord, str]],
    changes: dict[str, str], candidates: list[FindingRecord], token_fn,
    owner: str, repo: str, head_sha: str, pr_number: int, apply_commit, reply,
) -> dict[str, Any]:
    """Commit prepared changes once and report every applied or skipped finding."""
    if not changes:
        reply(_build_reply(applied, skipped))
        return {
            "verb": "fix", "status": "no_op", "reason": "nothing_applies",
            "applied": [],
            "skipped": [
                {"path": f.path, "line": f.line, "rule": f.rule, "reason": reason}
                for f, reason in skipped
            ],
        }

    new_head = apply_commit(
        token_fn, owner, repo, head_sha, changes,
        f"Apply grug autofix suggestions on PR #{pr_number}", pr_number,
    )
    if new_head is None:
        failed = [(candidate, "commit could not be created") for candidate in candidates]
        reply(_build_reply([], failed))
        return {
            "verb": "fix", "status": "skip", "reason": "commit_failed",
            "applied": [],
            "skipped": [
                {"path": f.path, "line": f.line, "rule": f.rule,
                 "reason": "commit could not be created"}
                for f in candidates
            ],
        }

    reply(_build_reply(applied, skipped))
    return {
        "verb": "fix", "status": "dispatched",
        "applied": [{"path": f.path, "line": f.line, "rule": f.rule} for f in applied],
        "skipped": [
            {"path": f.path, "line": f.line, "rule": f.rule, "reason": reason}
            for f, reason in skipped
        ],
    }


def run_fix(
    *,
    install_id: int,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    token_fn,
    get_session=None,
    fetch_file=None,
    apply_commit=None,
    post_reply=None,
) -> dict[str, Any]:
    """Apply all unresolved committable suggestions as one bot commit.

    Returns a status dict. On success the commit is pushed and a reply is
    posted naming every applied and skipped suggestion.
    """
    if get_session is None:
        get_session = _default_get_session
    if fetch_file is None:
        fetch_file = _default_fetch_file
    if apply_commit is None:
        apply_commit = _default_apply_commit
    reply = post_reply or (
        lambda body: _default_post_reply(
            token_fn, owner, repo, pr_number, body,
        )
    )

    _, candidates, early_result = _load_candidates(
        install_id=install_id, repo=repo, pr_number=pr_number,
        get_session=get_session, reply=reply,
    )
    if early_result is not None:
        return early_result

    applied, skipped, changes = _prepare_changes(
        candidates, token_fn=token_fn, owner=owner, repo=repo,
        head_sha=head_sha, fetch_file=fetch_file,
    )

    # Apply all valid changes as ONE commit and report the exact outcome.
    return _commit_and_report(
        applied=applied, skipped=skipped, changes=changes, candidates=candidates,
        token_fn=token_fn, owner=owner, repo=repo, head_sha=head_sha,
        pr_number=pr_number, apply_commit=apply_commit, reply=reply,
    )


def _build_reply(
    applied: list[FindingRecord], skipped: list[tuple[FindingRecord, str]],
) -> str:
    """Build the fix result reply naming every applied and skipped suggestion."""
    lines: list[str] = ["Grug autofix results:"]
    if applied:
        lines.append("")
        lines.append("Applied:")
        for f in applied:
            lines.append(f"- `{f.path}:{f.line}` [{f.rule}]")
    else:
        lines.append("")
        lines.append("Applied: (none)")
    if skipped:
        lines.append("")
        lines.append("Skipped:")
        for f, reason in skipped:
            lines.append(f"- `{f.path}:{f.line}` [{f.rule}] - {reason}")
    else:
        lines.append("")
        lines.append("Skipped: (none)")
    lines.append("")
    lines.append("So speaks Grug.")
    return "\n".join(lines)
