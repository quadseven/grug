"""`/grug accept-removal <path>`: a maintainer answers a deleted guard (#1405).

A deletion-only PR that removes a CI workflow, CODEOWNERS, ruleset config,
a security, policy or leak-scan file, or a test whose code stays gets a
blocking `removed-guarded-path` finding (#1401). The file is gone, so no
inline thread can answer it. This command does.

Trust rule, all of it read from the webhook payload, never the comment
text, and each part failing closed when it is missing:
- the comment's author is a `User` account, never a Bot, an Organization
  or a Mannequin, and the comment was not posted through a GitHub App;
- the comment's author is the delivery's sender;
- its author_association is OWNER, MEMBER or COLLABORATOR;
- the collaborator permission API says admin, maintain or write. The PR
  author shortcut that `/grug improve` takes does not apply, though the
  PR author may accept when they pass every check (a solo maintainer
  authors their own PRs).

The acceptance is stored against the PR's head SHA when the command is
handled, so a new head finds no acceptance and the finding comes back.
Accepting a path clears only that path's `removed-guarded-path` finding.
Elder then re-reviews, so the check updates without another step.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.accept_removal")

TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_MAX_PATH = 1024
USAGE = "Usage: `/grug accept-removal <path>`, naming one deleted file."


def removal_path(arg: str) -> str:
    """The path the command names: the backticked text when it starts with
    a backtick, else the first word. A leading `./` is dropped. Empty when
    there is none, or when it holds a backtick, `<`, `>` or a control
    character."""
    text = (arg or "").strip()
    if text.startswith("`"):
        end = text.find("`", 1)
        path = text[1:end] if end > 0 else text[1:]
    else:
        path = text.split(None, 1)[0] if text else ""
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    # The path is echoed in a code span; one that could break out of it,
    # or that no git path looks like, names nothing.
    if len(path) > _MAX_PATH or any(c in path for c in "`<>") or not path.isprintable():
        return ""
    return path


def trusted_acceptor(payload: dict[str, Any]) -> tuple[str, str]:
    """(login, "") when the comment's author may accept a removal, else
    ("", reason). Reads only the payload's comment and sender fields."""
    comment = payload.get("comment") or {}
    user = comment.get("user") or {}
    login = str(user.get("login") or "")
    sender = str((payload.get("sender") or {}).get("login") or "")
    if not login or login != sender:
        return "", "comment author unconfirmed"
    if not isinstance(comment.get("id"), int):
        return "", "comment id unknown"
    if user.get("type") != "User":
        return "", "comment author is not a user account"
    if comment.get("performed_via_github_app"):
        return "", "comment posted through a GitHub App"
    if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
        return "", "comment author is not an owner, member or collaborator"
    return login, ""


def run_accept_removal(
    arg: str, *, install_id: int, owner: str, repo: str, pr_number: int,
    comment_id: int, head_sha: str, actor: str, token_fn,
    record: Callable[..., None] | None = None,
    enqueue: Callable[..., None] | None = None,
    claim: Callable[[str], bool] | None = None,
    reply: Callable[[str], bool] | None = None,
) -> dict[str, str]:
    """Record the acceptance on `head_sha`, then re-run Elder.

    The dispatcher has already applied `trusted_acceptor` and the write
    check; `actor` is the login that passed them. Record, then enqueue,
    then claim: a failed record or enqueue leaves the comment unclaimed and
    is answered, so nothing is dropped silently. The enqueue carries the
    comment id as its dedup key, so a re-run that ran a moment earlier
    cannot swallow this one inside the queue's dedup window."""
    from interactive import _reply  # type: ignore

    if reply is None:
        def reply(body: str) -> bool:
            return _reply(token_fn, owner, repo, pr_number, body)
    path = removal_path(arg)
    if not path:
        reply(USAGE)
        return {"status": "no_op", "reason": "no path named", "verb": "accept-removal"}
    if not (head_sha and actor):
        return {"status": "skip", "reason": "head or actor unknown", "verb": "accept-removal"}
    if record is None:
        from adapters.pg_install_store import record_removal_acceptance  # type: ignore
        record = record_removal_acceptance
    if enqueue is None:
        from rerun import enqueue_rerun  # type: ignore
        enqueue = enqueue_rerun
    if claim is None:
        from adapters.pg_install_store import claim_delivery  # type: ignore
        claim = claim_delivery
    repo_full = f"{owner}/{repo}"
    coords = {"repo": repo_full, "pr": pr_number, "head_sha": head_sha[:8]}
    try:
        record(
            install_id=install_id, repo=repo_full, pr_number=pr_number,
            head_sha=head_sha, path=path, accepted_by=actor,
            comment_id=comment_id,
        )
    except Exception as e:  # noqa: BLE001 - answered, left unclaimed for a retry
        log.warning("accept_removal_record_failed", extra={**coords, "kind": type(e).__name__})
        reply("Grug could not record that acceptance, so the removal still "
              "blocks. Comment the command again.")
        return {"status": "skip", "reason": "record_failed", "verb": "accept-removal"}
    log.info("accept_removal_recorded", extra={**coords, "actor": actor})
    try:
        enqueue(install_id=install_id, repo=repo_full, pr_number=pr_number,
                persona="code_reviewer", dedup_key=f"accept-removal:{comment_id}")
    except Exception as e:  # noqa: BLE001 - answered, left unclaimed for a retry
        log.warning("accept_removal_enqueue_failed", extra={**coords, "kind": type(e).__name__})
        reply(f"Grug recorded `{actor}` accepting the removal of `{path}` on "
              f"`{head_sha[:8]}`, but could not queue Elder. Comment `/grug "
              "improve` to re-review.")
        return {"status": "skip", "reason": "enqueue_failed", "verb": "accept-removal"}
    if not claim(f"grugcmd:{comment_id}"):
        return {"status": "no_op", "reason": "comment already handled", "verb": "accept-removal"}
    reply(f"Grug recorded `{actor}` accepting the removal of `{path}` on "
          f"`{head_sha[:8]}`. Elder is re-reviewing this PR. A new push needs "
          "a new acceptance. So speaks Grug.")
    return {"status": "dispatched", "verb": "accept-removal", "persona": "code_reviewer"}
