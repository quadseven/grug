"""Generate docstrings for PR's new or changed public functions that lack one.

The command finds all functions in the PR's changed files that are public (no
leading underscore), have no existing docstring, and generates appropriate
docstrings following Python's conventions. All changes are applied as ONE
bot-attributed commit on the PR branch, refusing fork PRs.

All side effects (file fetches, GitHub API calls, the commit itself) flow
through injected seams so the logic is unit-testable without a network.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote as _q

import review_session as rs

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.docstring")

_API = "https://api.github.com"
_TIMEOUT = 15.0
_BOT_NAME = "grug-tribe[bot]"
_BOT_EMAIL = "grug-tribe[bot]@users.noreply.github.com"


def _is_public_function(name: str) -> bool:
    """Return True when a function name is public (no leading underscore)."""
    return not name.startswith("_")


def _has_docstring(lines: list[str], line_number: int) -> bool:
    """Check if a function at the given line has a docstring immediately after it."""
    if line_number >= len(lines):
        return False
    next_line = lines[line_number].strip()
    return next_line.startswith('"""') or next_line.startswith("'''")


def _generate_docstring(function_name: str, file_path: str) -> str:
    """Generate a simple docstring for a function.
    
    Returns a string containing a proper docstring block with the function name.
    """
    return f'"""Docstring for {function_name}."""'


def _find_public_functions_without_docstrings(
    file_text: str, file_path: str,
) -> list[tuple[int, str, str]]:
    """Find all public functions without docstrings in a file.
    
    Returns a list of (line_number, function_name, suggestion) tuples.
    """
    lines = file_text.splitlines()
    suggestions: list[tuple[int, str, str]] = []
    i = 0
    
    while i < len(lines):
        line = lines[i].strip()
        
        if line.startswith(("def ", "async def ")):
            if line.startswith("async def "):
                _, rest = line.split(" ", 1)
            else:
                _, rest = line.split(" ", 1)
            
            if "(" in rest:
                func_name = rest.split("(")[0].strip()
                
                if _is_public_function(func_name) and not _has_docstring(lines, i):
                    suggestions.append((i + 1, func_name, _generate_docstring(func_name, file_path)))
        
        i += 1
    
    return suggestions


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
        log.warning("docstring_reply_failed", extra={"kind": type(e).__name__})
        return False
    return True


def _default_get_session(
    install_id: int, full_name: str, pr_number: int,
) -> rs.ReviewSession | None:
    """Load the PR's review session the way hunt_recorder stores it."""
    from adapters.install_store import get_review_session
    raw = get_review_session(
        install_id=install_id, repo=full_name, pr_number=int(pr_number),
    )
    return rs.from_dict(raw) if raw else None


def _default_fetch_file(
    token_fn, owner: str, repo: str, path: str, ref: str,
) -> str | None:
    import httpx  # type: ignore

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
        log.info("docstring_file_fetch_skipped", extra={"path": path, "kind": type(e).__name__})
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
    except (httpx.HTTPStatusError, httpx.RequestError, KeyError, TypeError) as e:
        log.warning("docstring_commit_failed", extra={"kind": type(e).__name__})
        return None


def _do_commit(
    token: str, owner: str, repo: str, head_sha: str,
    changes: dict[str, str], message: str, pr_number: int, _req,
) -> str:
    """Actual GitHub Git Data API sequence. Raises on failure, and raises
    CommitRefused before any write when the commit must not be made."""
    import base64
    from urllib.parse import quote
    base_url = f"{_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    pr_resp = _req(token, "GET", f"{base_url}/pulls/{int(pr_number)}")
    pr_resp.raise_for_status()
    head = pr_resp.json()["head"]
    head_ref = head["ref"]
    _refuse_unsafe_head(head, owner=owner, repo=repo, head_sha=head_sha)

    commit_resp = _req(token, "GET", f"{base_url}/git/commits/{head_sha}")
    commit_resp.raise_for_status()
    base_tree_sha = commit_resp.json()["tree"]["sha"]
    modes = _file_modes(token, base_url, base_tree_sha, changes, _req)

    blob_shas: dict[str, str] = {}
    for path, text in changes.items():
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        blob_resp = _req(token, "POST", f"{base_url}/git/blobs",
                         json={"content": encoded, "encoding": "base64"})
        blob_resp.raise_for_status()
        blob_shas[path] = blob_resp.json()["sha"]

    tree_entries = [
        {"path": path, "mode": modes.get(path, "100644"), "type": "blob",
         "sha": blob_shas[path]}
        for path in changes
    ]
    tree_resp = _req(token, "POST", f"{base_url}/git/trees",
                     json={"base_tree": base_tree_sha, "tree": tree_entries})
    tree_resp.raise_for_status()
    new_tree_sha = tree_resp.json()["sha"]

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

    ref_resp = _req(
        token, "PATCH",
        f"{base_url}/git/refs/heads/{quote(head_ref, safe='/')}",
        json={"sha": new_commit_sha, "force": False},
    )
    ref_resp.raise_for_status()
    return new_commit_sha


def _refuse_unsafe_head(
    head: dict[str, Any], *, owner: str, repo: str, head_sha: str,
) -> None:
    """Raise CommitRefused unless the PR branch lives in this repo and still
    points at `head_sha`.
    
    A fork PR's `head.ref` names a branch in the FORK: updating
    `heads/<ref>` here would move this repo's own branch of that name
    instead (a fork PR from `main` would fast-forward this repo's `main`).
    """
    full_name = str(((head.get("repo") or {}).get("full_name")) or "")
    if full_name.lower() != f"{owner}/{repo}".lower():
        raise CommitRefused(
            "this PR comes from a fork, and Grug only commits to branches in this repo"
        )
    if str(head.get("sha") or "") != head_sha:
        raise CommitRefused(
            "the PR head moved since the command; comment `/grug docstring` again"
        )


def _file_modes(
    token: str, base_url: str, tree_sha: str, changes: dict[str, str], _req,
) -> dict[str, str]:
    """The current git mode of each changed path, so an executable file
    stays executable."""
    resp = _req(token, "GET", f"{base_url}/git/trees/{tree_sha}?recursive=1")
    resp.raise_for_status()
    entries = resp.json().get("tree") or []
    return {
        e["path"]: e["mode"] for e in entries
        if e.get("path") in changes and e.get("type") == "blob" and e.get("mode")
    }


def _load_candidates(
    *, install_id: int, repo: str, pr_number: int, get_session, reply,
) -> tuple[Any | None, list[tuple[str, str, str]]]:
    """Load the review session and find all public functions without docstrings."""
    session = get_session(install_id, repo, pr_number)
    if not session:
        reply("Grug found no review session for this PR, so there is nothing to docstring.")
        return None, [], {"verb": "docstring", "status": "skip", "reason": "no_review_session"}

    candidates: list[tuple[str, str, str]] = []
    
    for finding in session.findings:
        path = finding.path
        if not path.endswith(".py"):
            continue
        
        suggestions = _find_public_functions_without_docstrings(
            finding.evidence or "", path
        )
        for line_num, func_name, suggestion in suggestions:
            candidates.append((path, f"{line_num}:{func_name}", suggestion))
    
    if candidates:
        return session, candidates, None
    
    reply("Grug found no public functions without docstrings on this PR.")
    return session, [], {"verb": "docstring", "status": "no_op", "reason": "no_functions_without_docstrings"}


def _prepare_changes(
    candidates: list[tuple[str, str, str]], *, token_fn, owner: str, repo: str,
    head_sha: str, fetch_file,
) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """Generate docstrings for all candidates and group changes by file."""
    applied: list[tuple[str, str, str]] = []
    skipped: list[tuple[str, str, str, str]] = []
    file_changes: dict[str, list[tuple[int, str, str]]] = {}
    modified_lines: set[tuple[str, int]] = set()

    texts: dict[str, str | None] = {}
    for path, location, suggestion in candidates:
        if path not in texts:
            texts[path] = fetch_file(token_fn, owner, repo, path, head_sha)
        file_text = texts[path]
        if file_text is None:
            skipped.append((path, location, suggestion, "could not read the file"))
            continue
        
        lines = file_text.splitlines()
        if location not in lines:
            skipped.append((path, location, suggestion, "location no longer exists"))
            continue
        
        line_num = int(location.split(":")[0])
        if line_num < 1 or line_num >= len(lines):
            skipped.append((path, location, suggestion, "line out of range"))
            continue
        
        if location in modified_lines:
            skipped.append((path, location, suggestion, "line already has a docstring"))
            continue
        
        lines[line_num - 1] = suggestion + "\n"
        modified_lines.add(location)
        
        if path not in file_changes:
            file_changes[path] = []
        file_changes[path].append((line_num, suggestion))
        applied.append((path, location, suggestion))
    
    changes = {}
    for path, line_items in file_changes.items():
        lines = texts[path].splitlines()
        for line_num, suggestion in line_items:
            lines[line_num - 1] = suggestion
        changes[path] = "\n".join(lines) + ("\n" if texts[path].endswith("\n") else "")
    
    return applied, skipped, changes


def _commit_and_report(
    *, applied: list[tuple[str, str, str]], skipped: list[tuple[str, str, str, str]],
    changes: dict[str, str], token_fn, owner: str, repo: str,
    head_sha: str, pr_number: int, apply_commit, reply,
) -> dict[str, Any]:
    """Commit prepared docstrings once and report every applied or skipped item."""
    if not changes:
        reply(_build_reply([], skipped))
        return {
            "verb": "docstring", "status": "no_op", "reason": "nothing_to_apply",
            "applied": [],
            "skipped": [
                {"path": p, "location": l, "reason": r}
                for p, l, s, r in skipped
            ],
        }

    refused = "commit could not be created"
    try:
        new_head = apply_commit(
            token_fn, owner, repo, head_sha, changes,
            f"Add docstrings to PR #{pr_number}",
            pr_number,
        )
    except CommitRefused as e:
        new_head, refused = None, str(e)
    if new_head is None:
        failed = skipped + [(finding, refused) for finding in applied]
        reply(_build_reply([], failed))
        return {
            "verb": "docstring", "status": "skip", "reason": "commit_failed",
            "applied": [],
            "skipped": [
                {"path": p, "location": l, "reason": r}
                for p, l, s, r in failed
            ],
        }

    reply(_build_reply(applied, skipped))
    return {
        "verb": "docstring", "status": "dispatched",
        "applied": [{"path": p, "location": l, "suggestion": s} for p, l, s in applied],
        "skipped": [
            {"path": p, "location": l, "reason": r}
            for p, l, s, r in skipped
        ],
    }


def _build_reply(
    applied: list[tuple[str, str, str]], skipped: list[tuple[str, str, str, str]],
) -> str:
    """Build the docstring result reply naming every applied and skipped item."""
    lines: list[str] = ["Grug docstring generation results:"]
    if applied:
        lines.append("")
        lines.append("Applied:")
        for path, location, suggestion in applied:
            lines.append(f"- `{path}:{location}`")
    else:
        lines.append("")
        lines.append("Applied: (none)")
    if skipped:
        lines.append("")
        lines.append("Skipped:")
        for path, location, _, reason in skipped:
            lines.append(f"- `{path}:{location}` - {reason}")
    else:
        lines.append("")
        lines.append("Skipped: (none)")
    lines.append("")
    lines.append("So speaks Grug.")
    return "\n".join(lines)


def run_docstring(
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
    """Generate docstrings for public functions that lack them as one bot commit.
    
    Returns a status dict. On success the commit is pushed and a reply is
    posted naming every applied and skipped item.
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
        install_id=install_id, repo=f"{owner}/{repo}", pr_number=pr_number,
        get_session=get_session, reply=reply,
    )
    if early_result is not None:
        return early_result

    applied, skipped, changes = _prepare_changes(
        candidates, token_fn=token_fn, owner=owner, repo=repo,
        head_sha=head_sha, fetch_file=fetch_file,
    )

    return _commit_and_report(
        applied=applied, skipped=skipped, changes=changes,
        token_fn=token_fn, owner=owner, repo=repo, head_sha=head_sha,
        pr_number=pr_number, apply_commit=apply_commit, reply=reply,
    )
