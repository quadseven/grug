"""GitHub PR Reviews API client — post reviews + inline comments.

Wraps `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` for the
review personas. Supports three events:

  - APPROVE: Elder's verdict on a clean head (#1758), so a merge gate
    that reads review state can count it.
  - REQUEST_CHANGES: a blocking finding stands. It stays the reviewer's
    standing verdict until the same reviewer approves or the review is
    dismissed. A new push does NOT clear it; see `dismiss_review`.
  - COMMENT: findings with no verdict (a degraded or superseded run, or
    a persona that never approves).

Only Elder chooses APPROVE (`personas.review_publish.verdict_event`);
Guard and Smasher keep COMMENT/REQUEST_CHANGES. `PENDING` is not
modeled: it creates a draft review that never publishes, which no
persona has use for. Reject it at construction so the bad payload never
leaves the process (GH 422s on submit either way, but failing locally is
faster).

Mirrors `github_checks_client.py` transport shape per ADR-0001;
retry/401-handling lives in the `with_install_token_retry` caller,
not here.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Literal, get_args
from urllib.parse import quote

import httpx

_GH_API = "https://api.github.com"
_log = logging.getLogger(__name__)
# A single review's inline comments are finding-bounded, but paginate anyway
# so a large diff's >100 comments aren't silently dropped (the #189 lesson).
# 10 × 100 = 1000 comments in one review is implausible; a cap-hit is logged.
_MAX_REVIEW_COMMENT_PAGES = 10

# Subset of GitHub's `event` enum the review personas use. PENDING is
# omitted on purpose - see module docstring.
ReviewEvent = Literal["APPROVE", "COMMENT", "REQUEST_CHANGES"]
# Derived from the Literal so adding a future event (e.g. "DISMISS")
# updates runtime validation automatically. Without `get_args`, the
# allowlist and the Literal silently drift.
_VALID_EVENTS: frozenset[str] = frozenset(get_args(ReviewEvent))

# GitHub's PR Reviews API caps the review body at 65536 characters
# (UTF-8). Longer payloads are 422'd. Guarded at ReviewResult
# construction.
_MAX_BODY_CHARS: int = 65536


@dataclass(frozen=True)
class InlineComment:
    """One inline review comment, pinned to a file + new-side line.

    `line` references the NEW side of the diff per the Elder
    hallucination filter (`DiffHunk.new_lines`). GitHub 422s on
    line=0.

    Uses `raise ValueError` (not `assert`) so `python -O` can't strip
    the guard — payloads built from LLM output cross a trust boundary
    and the guard must survive optimization.
    """

    path: str
    line: int
    body: str
    # Explicit RIGHT: `line` is always the NEW side here, and the docs do
    # not promise a default - an implicit LEFT would anchor committable
    # suggestion blocks (#553) on the wrong side of the diff.
    side: Literal["LEFT", "RIGHT"] = "RIGHT"

    def __post_init__(self) -> None:
        if self.line < 1:
            raise ValueError(
                f"InlineComment.line must be >= 1 (got {self.line}); "
                "GitHub's PR Reviews API 422s on line=0"
            )
        if not self.path:
            raise ValueError("InlineComment.path must be non-empty")


@dataclass(frozen=True)
class ReviewResult:
    """A complete PR review payload — top-level body + inline comments.

    `commit_id` pins the review to a specific PR head. Without it, GH
    attaches the review to whatever the PR head is at POST time, which
    races against new pushes — the review can show up on the wrong
    commit.

    `event` is the cross-field invariant the constructor enforces (see
    module docstring for why PENDING is omitted).
    """

    commit_id: str
    event: ReviewEvent
    body: str
    comments: tuple[InlineComment, ...]

    def __post_init__(self) -> None:
        if self.event not in _VALID_EVENTS:
            raise ValueError(
                f"ReviewResult.event must be one of {sorted(_VALID_EVENTS)} "
                f"(got {self.event!r}). PENDING is not supported."
            )
        if not self.commit_id:
            raise ValueError("ReviewResult.commit_id must be non-empty")
        # GitHub caps review body at 65536 chars; guard at construction
        # so the bad payload never crosses the wire.
        if len(self.body) > _MAX_BODY_CHARS:
            raise ValueError(
                f"ReviewResult.body length {len(self.body)} exceeds "
                f"GitHub's {_MAX_BODY_CHARS}-char limit"
            )


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
    Same 401-propagation pattern as `post_check_run`.

    `pull_number` and `result` are keyword-only on purpose —
    `post_check_run` takes a `head_sha` baked into `CheckRunResult`,
    but `pull_number` is an extra positional int we don't want to
    confuse with `result`. The kw-only barrier is a deliberate ADR-0001
    divergence (the only one) that prevents the
    `post_review("tok", "o", "r", 42)` foot-gun.
    """
    # `asdict` recursively converts ReviewResult + nested InlineComments
    # into the exact dict shape GitHub expects (commit_id/event/body
    # /comments with path/line/body). Manual marshalling would have to
    # stay in lockstep with field renames; this stays correct by
    # construction.
    resp = httpx.post(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{pull_number}/reviews",
        json=asdict(result),
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def update_review_body(
    install_token: str,
    owner: str,
    repo: str,
    *,
    pull_number: int,
    review_id: int,
    body: str,
) -> None:
    """Update a submitted review's body while preserving its verdict and comments."""
    resp = httpx.put(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{pull_number}/reviews/{review_id}",
        json={"body": body},
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    resp.raise_for_status()


def list_reviews(
    install_token: str,
    owner: str,
    repo: str,
    *,
    pull_number: int,
) -> list[dict]:
    """List every review on a PR, oldest first.

    Paginates the same way as `get_review_comments`. Does NOT catch 401:
    `with_install_token_retry` at the call site owns invalidate + retry.
    """
    out: list[dict] = []
    for page in range(1, _MAX_REVIEW_COMMENT_PAGES + 1):
        resp = httpx.get(
            f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{pull_number}/reviews",
            params={"per_page": 100, "page": page},
            headers={
                "Authorization": f"Bearer {install_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, list):
            break
        out.extend(body)
        if len(body) < 100:
            break
    else:
        _log.warning(
            "list_reviews_page_cap_hit",
            extra={"repo": f"{owner}/{repo}", "pull_number": pull_number,
                   "max_pages": _MAX_REVIEW_COMMENT_PAGES},
        )
    return out


def dismiss_review(
    install_token: str,
    owner: str,
    repo: str,
    *,
    pull_number: int,
    review_id: int,
    message: str,
) -> dict:
    """Dismiss one review (`PUT .../reviews/{id}/dismissals`).

    A REQUEST_CHANGES review stays the reviewer's standing verdict until
    the same reviewer approves or the review is dismissed; a later COMMENT
    review does not replace it. Elder dismisses its own block once a clean
    run approves (#1758), and its own approval once a later run cannot
    vouch for the head. Needs `pull_requests: write`, which the app already
    holds.
    """
    resp = httpx.put(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{pull_number}/reviews/{review_id}/dismissals",
        json={"message": message, "event": "DISMISS"},
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_review_comments(
    install_token: str,
    owner: str,
    repo: str,
    *,
    pull_number: int,
    review_id: int,
) -> list[dict]:
    """List the inline comments a posted review created.

    `post_review` returns the review object (with its `id`) but NOT the
    per-comment IDs, which reaction-polling (#247) needs to key each
    `CommentRecord`. This fetches them via
    `GET /repos/{o}/{r}/pulls/{n}/reviews/{review_id}/comments`.

    Paginates (`per_page=100`, stops on a short page) up to
    `_MAX_REVIEW_COMMENT_PAGES` — so a large diff's >100 inline comments are
    NOT silently truncated (the #189 lesson; mirrors `_fetch_pr_review_comments`).
    Does NOT catch 401: `with_install_token_retry` at the call site owns
    invalidate + retry, same pattern as `post_review` / `post_check_run`.
    """
    out: list[dict] = []
    for page in range(1, _MAX_REVIEW_COMMENT_PAGES + 1):
        resp = httpx.get(
            f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{pull_number}/reviews/{review_id}/comments",
            params={"per_page": 100, "page": page},
            headers={
                "Authorization": f"Bearer {install_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, list):
            break
        out.extend(body)
        if len(body) < 100:
            break
    else:
        _log.warning(
            "get_review_comments_page_cap_hit",
            extra={"repo": f"{owner}/{repo}", "review_id": review_id,
                   "max_pages": _MAX_REVIEW_COMMENT_PAGES},
        )
    return out
