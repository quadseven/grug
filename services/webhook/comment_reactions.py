"""Emoji acknowledgement on a `/grug` command comment (#695).

GitHub lets an app react to a comment with a fixed set of emoji. Grug puts
`eyes` on a command the moment it is heard and swaps it for `+1` (done) or
`-1` (failed) once the work resolves. Two small calls, no comment noise.

Best-effort by design: the reaction is a sign, never the work. A refused or
failed call is logged and answered with None/False, and never raised, so a
reaction problem can not fail the command it decorates.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote as _q

import httpx

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.comment_reactions")

_API = "https://api.github.com"
_TIMEOUT = 10.0

EYES = "eyes"
DONE = "+1"
FAILED = "-1"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _url(owner: str, repo: str, comment_id: int) -> str:
    return (
        f"{_API}/repos/{_q(owner, safe='')}/{_q(repo, safe='')}"
        f"/issues/comments/{comment_id}/reactions"
    )


def add_reaction(
    token: str, owner: str, repo: str, comment_id: int, content: str,
) -> int | None:
    """React to a comment. Returns the reaction id, or None when GitHub
    refused or answered without one."""
    try:
        resp = httpx.post(
            _url(owner, repo, comment_id),
            json={"content": content},
            headers=_headers(token),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return int(resp.json()["id"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        log.warning(
            "comment_reaction_add_failed",
            extra={"content": content, "kind": type(e).__name__},
        )
        return None


def remove_reaction(
    token: str, owner: str, repo: str, comment_id: int, reaction_id: int,
) -> bool:
    """Take one reaction off a comment. True when it is gone."""
    try:
        httpx.delete(
            f"{_url(owner, repo, comment_id)}/{int(reaction_id)}",
            headers=_headers(token),
            timeout=_TIMEOUT,
        ).raise_for_status()
    except httpx.HTTPError as e:
        log.warning(
            "comment_reaction_remove_failed", extra={"kind": type(e).__name__},
        )
        return False
    return True


def swap_reaction(
    token: str, owner: str, repo: str, comment_id: int,
    old_reaction_id: int | None, content: str,
) -> int | None:
    """Replace the `eyes` sign with the final one. The new reaction goes up
    first, so a failed removal leaves both signs rather than none."""
    new_id = add_reaction(token, owner, repo, comment_id, content)
    if new_id is not None and old_reaction_id:
        remove_reaction(token, owner, repo, comment_id, old_reaction_id)
    return new_id
