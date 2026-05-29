# MIRRORED — sibling at services/webhook/github_reviews_client.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""GitHub PR Reviews API client — post reviews + inline comments.

Wraps `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` for the
Elder (code-reviewer) persona. Supports two events:

  - COMMENT: advisory mode — reports findings but does not block merge.
  - REQUEST_CHANGES: blocking mode — blocks merge until a follow-up
    review approves or the PR is updated.

`APPROVE` is intentionally not modeled. Grug does not automatically
approve PRs (it would short-circuit human review and is not what an
LLM-backed reviewer should claim). `PENDING` is also out — that creates
a draft review that never publishes, which the persona has no use for.
Reject both shapes at construction so the bad payload never leaves the
process (GH 422s on submit either way, but failing locally is faster).

Mirrors `github_checks_client.py` for transport + retry shape per
ADR-0001 (rule-of-three deferred shared package).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

_GH_API = "https://api.github.com"

# Subset of GitHub's `event` enum the Elder persona uses. APPROVE +
# PENDING omitted on purpose — see module docstring.
ReviewEvent = Literal["COMMENT", "REQUEST_CHANGES"]
_VALID_EVENTS: frozenset[str] = frozenset(("COMMENT", "REQUEST_CHANGES"))


@dataclass(frozen=True, slots=True)
class InlineComment:
    """One inline review comment, pinned to a file + new-side line.

    `line` references the NEW side of the diff per the Elder
    hallucination filter (`DiffHunk.new_lines`). GitHub 422s on
    line=0, so the assertion catches a malformed payload at parse
    time rather than at the POST.
    """

    path: str
    line: int
    body: str

    def __post_init__(self) -> None:
        assert self.line >= 1, (
            f"InlineComment.line must be >= 1 (got {self.line}); "
            "GitHub's PR Reviews API 422s on line=0"
        )
        assert self.path, "InlineComment.path must be non-empty"


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """A complete PR review payload — top-level body + inline comments.

    `commit_id` pins the review to a specific PR head. Without it, GH
    attaches the review to whatever the PR head is at POST time, which
    races against new pushes — the review can show up on the wrong
    commit.

    `event` is the cross-field invariant the constructor enforces (see
    module docstring for why APPROVE/PENDING are omitted).
    """

    commit_id: str
    event: ReviewEvent
    body: str
    comments: tuple[InlineComment, ...]

    def __post_init__(self) -> None:
        if self.event not in _VALID_EVENTS:
            raise ValueError(
                f"ReviewResult.event must be one of {sorted(_VALID_EVENTS)} "
                f"(got {self.event!r}). APPROVE/PENDING are not supported "
                "for the Elder persona."
            )
        assert self.commit_id, "ReviewResult.commit_id must be non-empty"


def post_review(
    install_token: str,
    owner: str,
    repo: str,
    *,
    pull_number: int,
    result: ReviewResult,
) -> dict:
    """POST a PR review.

    Does NOT catch 401 — the `with_install_token_retry` wrapper at the
    call site is responsible for invalidating the cache and retrying.
    Same pattern as `post_check_run`.
    """
    body = {
        "commit_id": result.commit_id,
        "event": result.event,
        "body": result.body,
        "comments": [
            {"path": c.path, "line": c.line, "body": c.body}
            for c in result.comments
        ],
    }
    resp = httpx.post(
        f"{_GH_API}/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
        json=body,
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()
