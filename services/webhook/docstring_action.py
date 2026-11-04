"""Generate docstrings for changed public functions and commit them once."""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import httpx
from personas.code_reviewer.diff_parser import DiffParseError, parse_diff
from personas.code_reviewer.docstrings import (
    DocstringTarget,
    find_docstring_targets,
    insert_docstring,
)

import autofix

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.docstrings")
_API = "https://api.github.com"


def _fetch_diff(token_fn, owner: str, repo: str, pr_number: int) -> str:
    def fetch(token: str) -> str:
        response = httpx.get(
            f"{_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/pulls/{int(pr_number)}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.diff",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=autofix._TIMEOUT,
        )
        response.raise_for_status()
        return response.text

    return token_fn(fetch)


def _generate_docstring(target: DocstringTarget, source: str) -> str:
    import llm_client

    config = llm_client._cave_judge_config()
    if config is None:
        raise RuntimeError("docstring model is not configured")
    lines = source.splitlines()
    node_source = "\n".join(lines[target.line - 1:target.end_line])
    response = llm_client._call_backend(config, [
        {
            "role": "system",
            "content": (
                "Write one accurate Python docstring for the named function. "
                "Return only its body text, without quote delimiters or Markdown. "
                "Do not invent behavior that is not shown by the code."
            ),
        },
        {
            "role": "user",
            "content": f"Function: {target.name}\n\n{node_source[:12000]}",
        },
    ])
    response.raise_for_status()
    content = str(
        (response.json().get("choices") or [{}])[0]
        .get("message", {}).get("content", "")
    ).strip()
    if content.startswith("```"):
        rows = content.splitlines()
        content = "\n".join(rows[1:-1] if rows[-1].strip() == "```" else rows[1:])
    return content.strip()


def _prepare(
    targets: list[DocstringTarget], file_contents: dict[str, str], generate,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    updated: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    for target in sorted(targets, key=lambda item: (item.path, -item.line)):
        source = updated.get(target.path, file_contents[target.path])
        try:
            docstring = generate(target, source)
            updated[target.path] = insert_docstring(
                source, line=target.line, docstring=docstring,
            )
        except Exception as exc:  # noqa: BLE001 - report every candidate failure
            log.warning(
                "docstring_candidate_skipped",
                extra={"path": target.path, "line": target.line,
                       "kind": type(exc).__name__},
            )
            skipped.append((
                f"{target.path}:{target.line} {target.name}",
                f"generation failed ({type(exc).__name__})",
            ))
    return updated, skipped


def _reply(changed: list[str], skipped: list[tuple[str, str]]) -> str:
    lines = ["Grug docstring results:", "", "Changed files:"]
    lines.extend(f"- `{path}`" for path in changed)
    if not changed:
        lines.append("- (none)")
    lines.extend(["", "Skipped:"])
    lines.extend(f"- `{item}` - {reason}" for item, reason in skipped)
    if not skipped:
        lines.append("- (none)")
    lines.extend(["", "So speaks Grug."])
    return "\n".join(lines)


def run_docstrings(
    *, install_id: int, owner: str, repo: str, pr_number: int, head_sha: str,
    token_fn, post_reply=None, fetch_diff=None, fetch_file=None,
    generate=None, apply_commit=None,
) -> dict:
    """Generate eligible docs, report skips, and create at most one commit."""
    del install_id
    reply = post_reply or (
        lambda body: autofix._default_post_reply(
            token_fn, owner, repo, pr_number, body,
        )
    )
    fetch_diff = fetch_diff or _fetch_diff
    fetch_file = fetch_file or autofix._default_fetch_file
    generate = generate or _generate_docstring
    apply_commit = apply_commit or autofix._default_apply_commit
    try:
        diff = fetch_diff(token_fn, owner, repo, pr_number)
    except (httpx.HTTPError, RuntimeError) as exc:
        reply(_reply([], [(f"PR #{pr_number}", f"could not read PR diff: {exc}")]))
        return {"verb": "docstrings", "status": "skip", "reason": "diff_unavailable"}

    try:
        paths = sorted({h.file_path for h in parse_diff(diff) if h.file_path.endswith(".py")})
    except DiffParseError:
        reason = "could not parse PR diff"
        reply(_reply([], [(f"PR #{pr_number}", reason)]))
        return {"verb": "docstrings", "status": "skip", "reason": "diff_unavailable"}
    contents: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    for path in paths:
        text = fetch_file(token_fn, owner, repo, path, head_sha)
        if text is not None:
            contents[path] = text
    targets, target_skips = find_docstring_targets(diff, contents)
    skipped.extend(target_skips)
    changes, generation_skips = _prepare(targets, contents, generate)
    skipped.extend(generation_skips)
    if not changes:
        reply(_reply([], skipped))
        return {"verb": "docstrings", "status": "no_op", "changed": [], "skipped": skipped}

    try:
        new_head = apply_commit(
            token_fn, owner, repo, head_sha, changes,
            f"Generate grug docstrings on PR #{pr_number}", pr_number,
        )
    except autofix.CommitRefused as exc:
        new_head = None
        commit_error = str(exc)
    except Exception as exc:  # noqa: BLE001 - name every uncommitted file
        log.warning(
            "docstring_commit_failed",
            extra={"pr": pr_number, "kind": type(exc).__name__},
        )
        new_head = None
        commit_error = f"commit failed ({type(exc).__name__})"
    else:
        commit_error = "commit could not be created"
    if new_head is None:
        skipped.extend((f"{path}", commit_error) for path in changes)
        reply(_reply([], skipped))
        return {"verb": "docstrings", "status": "skip", "reason": "commit_failed", "skipped": skipped}
    changed = sorted(changes)
    reply(_reply(changed, skipped))
    return {"verb": "docstrings", "status": "dispatched", "changed": changed, "skipped": skipped}
