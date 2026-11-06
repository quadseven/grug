"""Generate unit tests for uncovered changed paths and commit them once."""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import autofix
import httpx
from docstring_action import _fetch_diff
from personas.code_reviewer.diff_parser import DiffParseError, parse_diff
from personas.code_reviewer.test_generation import (
    TestTarget,
    find_test_targets,
    merge_generated_tests,
    test_path_for,
)
from personas.code_reviewer.test_paths import is_test_file

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.tests")
_API = "https://api.github.com"


def _fetch_pr_head(token_fn, owner: str, repo: str, pr_number: int) -> dict | None:
    """The PR's head dict, or None when it cannot be read (the commit
    seam's own fork check stays the backstop then)."""
    def fetch(token: str) -> dict | None:
        response = httpx.get(
            f"{_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/pulls/{int(pr_number)}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=autofix._TIMEOUT,
        )
        response.raise_for_status()
        return response.json().get("head")

    try:
        return token_fn(fetch)
    except Exception:  # noqa: BLE001 - unreadable head: commit seam decides
        log.warning("test_generation_head_fetch_failed", extra={"pr": pr_number})
        return None


def _generate_tests(target: TestTarget, source: str) -> str:
    import llm_client

    config = llm_client._cave_judge_config()
    if config is None:
        raise RuntimeError("test generation model is not configured")
    lines = source.splitlines()
    node_source = "\n".join(lines[target.line - 1:target.end_line])
    response = llm_client._call_backend(config, [
        {
            "role": "system",
            "content": (
                "Write pytest unit tests for the named Python function. "
                "Return only Python test code, without Markdown fences. "
                "Test the behavior the code shows; do not invent behavior, "
                "and import the function from its module."
            ),
        },
        {
            "role": "user",
            "content": (
                f"File: {target.path}\nFunction: {target.name}\n\n{node_source[:12000]}"
            ),
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
    targets: list[TestTarget], file_contents: dict[str, str],
    test_contents: dict[str, str | None], generate,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    updated: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    ordered = sorted(
        targets, key=lambda item: (item.test_path, item.path, item.line),
    )
    for target in ordered:
        source = file_contents[target.path]
        try:
            generated = generate(target, source)
            current = updated.get(
                target.test_path, test_contents.get(target.test_path),
            )
            updated[target.test_path] = merge_generated_tests(current, generated)
        except Exception as exc:  # noqa: BLE001 - report every candidate failure
            log.warning(
                "test_generation_candidate_skipped",
                extra={"path": target.path, "line": target.line,
                       "kind": type(exc).__name__},
            )
            skipped.append((
                f"{target.path}:{target.line} {target.name}",
                f"generation failed ({type(exc).__name__})",
            ))
    return updated, skipped


def _reply(changed: list[str], skipped: list[tuple[str, str]]) -> str:
    lines = ["Grug test generation results:", "", "Changed files:"]
    lines.extend(f"- `{path}`" for path in changed)
    if not changed:
        lines.append("- (none)")
    lines.extend(["", "Skipped:"])
    lines.extend(f"- `{item}` - {reason}" for item, reason in skipped)
    if not skipped:
        lines.append("- (none)")
    lines.extend(["", "So speaks Grug."])
    return "\n".join(lines)


def run_tests(
    *, install_id: int, owner: str, repo: str, pr_number: int, head_sha: str,
    token_fn, post_reply=None, fetch_diff=None, fetch_file=None,
    generate=None, apply_commit=None, fetch_pr_head=None,
) -> dict:
    """Generate tests for uncovered changed paths, report skips, and
    create at most one commit (the shared autofix commit path, which
    refuses fork PRs)."""
    del install_id
    reply = post_reply or (
        lambda body: autofix._default_post_reply(
            token_fn, owner, repo, pr_number, body,
        )
    )
    fetch_diff = fetch_diff or _fetch_diff
    fetch_file = fetch_file or autofix._default_fetch_file
    generate = generate or _generate_tests
    apply_commit = apply_commit or autofix._default_apply_commit
    fetch_pr_head = fetch_pr_head or _fetch_pr_head
    head = fetch_pr_head(token_fn, owner, repo, pr_number)
    if head is not None:
        head_repo = str(((head.get("repo") or {}).get("full_name")) or "")
        if head_repo.lower() != f"{owner}/{repo}".lower():
            reason = (
                "this PR comes from a fork, and Grug only commits to "
                "branches in this repo"
            )
            skipped = [(f"PR #{pr_number}", reason)]
            reply(_reply([], skipped))
            return {
                "verb": "tests", "status": "skip", "reason": "fork_pr",
                "skipped": skipped,
            }
    try:
        diff = fetch_diff(token_fn, owner, repo, pr_number)
    except (httpx.HTTPError, RuntimeError) as exc:
        reply(_reply([], [(f"PR #{pr_number}", f"could not read PR diff: {exc}")]))
        return {"verb": "tests", "status": "skip", "reason": "diff_unavailable"}

    try:
        paths = sorted({h.file_path for h in parse_diff(diff) if h.file_path.endswith(".py")})
    except DiffParseError:
        reason = "could not parse PR diff"
        reply(_reply([], [(f"PR #{pr_number}", reason)]))
        return {"verb": "tests", "status": "skip", "reason": "diff_unavailable"}
    contents: dict[str, str] = {}
    for path in paths:
        text = fetch_file(token_fn, owner, repo, path, head_sha)
        if text is not None:
            contents[path] = text
    # The test file mapped to each changed source is the coverage evidence
    # and the merge base, whether or not the PR itself touched it.
    for path in paths:
        if is_test_file(path):
            continue
        test_path = test_path_for(path)
        if test_path not in contents:
            text = fetch_file(token_fn, owner, repo, test_path, head_sha)
            if text is not None:
                contents[test_path] = text
    targets, skipped = find_test_targets(diff, contents)
    test_contents: dict[str, str | None] = {}
    for test_path in sorted({t.test_path for t in targets}):
        if test_path in contents:
            test_contents[test_path] = contents[test_path]
        else:
            test_contents[test_path] = fetch_file(
                token_fn, owner, repo, test_path, head_sha,
            )
    changes, generation_skips = _prepare(targets, contents, test_contents, generate)
    skipped.extend(generation_skips)
    if not changes:
        reply(_reply([], skipped))
        return {"verb": "tests", "status": "no_op", "changed": [], "skipped": skipped}

    try:
        new_head = apply_commit(
            token_fn, owner, repo, head_sha, changes,
            f"Generate grug unit tests on PR #{pr_number}", pr_number,
        )
    except autofix.CommitRefused as exc:
        new_head = None
        commit_error = str(exc)
    except Exception as exc:  # noqa: BLE001 - name every uncommitted file
        log.warning(
            "test_generation_commit_failed",
            extra={"pr": pr_number, "kind": type(exc).__name__},
        )
        new_head = None
        commit_error = f"commit failed ({type(exc).__name__})"
    else:
        commit_error = "commit could not be created"
    if new_head is None:
        skipped.extend((f"{path}", commit_error) for path in changes)
        reply(_reply([], skipped))
        return {"verb": "tests", "status": "skip", "reason": "commit_failed", "skipped": skipped}
    changed = sorted(changes)
    reply(_reply(changed, skipped))
    return {"verb": "tests", "status": "dispatched", "changed": changed, "skipped": skipped}
