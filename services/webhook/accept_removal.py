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
  author shortcut that `/grug improve` takes does not apply;
- the PR author may accept their own PR only as OWNER or with admin
  permission, so a solo maintainer still can, while plain write access
  accepts only someone else's PR.

The command names the head the maintainer reviewed
(`/grug accept-removal <path> <sha>`, at least 7 hex characters), and it
counts only while that is still the PR's head. The acceptance is stored
against that head, so a new head finds no acceptance and the finding comes
back. Each comment is claimed before anything is recorded, so a
redelivered comment records nothing.
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
MIN_SHA = 7
USAGE = (
    "Usage: `/grug accept-removal <path> <head-sha>`, naming one deleted file "
    f"and at least the first {MIN_SHA} characters of the head you reviewed."
)
_HEX = frozenset("0123456789abcdef")


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


def accept_args(arg: str) -> tuple[str, str]:
    """(path, head SHA prefix) from the command's argument. The SHA is the
    word after the path, lowercased; empty unless it is 7 to 40 hex
    characters."""
    text = (arg or "").strip()
    path = removal_path(text)
    if text.startswith("`"):
        end = text.find("`", 1)
        rest = text[end + 1:] if end > 0 else ""
    else:
        parts = text.split(None, 1)
        rest = parts[1] if len(parts) > 1 else ""
    words = rest.split()
    sha = words[0].strip("`").lower() if words else ""
    if not (MIN_SHA <= len(sha) <= 40 and set(sha) <= _HEX):
        sha = ""
    return path, sha


def accept_command(path: str, head_sha: str) -> str:
    """The exact command that accepts deleting `path` on `head_sha`."""
    return f"/grug accept-removal {path} {head_sha}"


def may_accept_own_pr(association: str, permission: str) -> bool:
    """A PR author accepts their own deletion only as the repo's OWNER or
    with admin permission. Plain write may accept only someone else's PR."""
    return association == "OWNER" or permission == "admin"


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
    """Claim the comment, check the named head, record the acceptance on
    it, then re-run Elder.

    The dispatcher has already applied `trusted_acceptor`, the write check
    and the self-acceptance rule; `actor` is the login that passed them.
    The claim comes FIRST: a redelivered comment must record nothing, so a
    comment is one acceptance at most. The command must name the head the
    maintainer reviewed, and that head must still be the PR's head. The
    enqueue carries the comment id as its dedup key, so a re-run that ran
    a moment earlier cannot swallow this one inside the queue's dedup
    window. A missing or non-callable seam fails closed: nothing is
    recorded and nothing is cleared."""
    out = {"verb": "accept-removal"}
    if not (head_sha and actor and comment_id):
        return {**out, "status": "skip", "reason": "head, actor or comment unknown"}
    from interactive import _reply  # type: ignore

    def _default_reply(body: str) -> bool:
        return _reply(token_fn, owner, repo, pr_number, body)

    say: Callable[[str], bool] = reply if callable(reply) else _default_reply
    if record is None:
        from adapters.pg_install_store import record_removal_acceptance  # type: ignore
        record = record_removal_acceptance
    if enqueue is None:
        from rerun import enqueue_rerun  # type: ignore
        enqueue = enqueue_rerun
    if claim is None:
        from adapters.pg_install_store import claim_delivery  # type: ignore
        claim = claim_delivery
    if not (callable(record) and callable(enqueue) and callable(claim)):
        log.error("accept_removal_seam_missing")
        return {**out, "status": "skip", "reason": "seam missing"}
    do_record: Callable[..., None] = record
    do_enqueue: Callable[..., None] = enqueue
    do_claim: Callable[[str], bool] = claim

    if not do_claim(f"grugcmd:{comment_id}"):
        return {**out, "status": "no_op", "reason": "comment already handled"}
    path, sha = accept_args(arg)
    if not path or not sha:
        say(USAGE)
        return {**out, "status": "no_op", "reason": "no path or head named"}
    repo_full = f"{owner}/{repo}"
    coords = {"repo": repo_full, "pr": pr_number, "head_sha": head_sha[:8]}
    if not head_sha.lower().startswith(sha):
        log.info("accept_removal_wrong_head", extra={**coords, "named": sha[:12]})
        say(f"The head is now `{head_sha[:12]}`, not `{sha[:12]}`, so Grug "
            "recorded nothing. Review the new head, then comment "
            f"`{accept_command(path, head_sha)}`.")
        return {**out, "status": "no_op", "reason": "head does not match"}
    try:
        do_record(
            install_id=install_id, repo=repo_full, pr_number=pr_number,
            head_sha=head_sha, path=path, accepted_by=actor,
            comment_id=comment_id,
        )
    except Exception as e:  # noqa: BLE001 - answered; a new comment retries
        log.warning("accept_removal_record_failed", extra={**coords, "kind": type(e).__name__})
        say("Grug could not record that acceptance, so the removal still "
            "blocks. Post the command again in a new comment.")
        return {**out, "status": "skip", "reason": "record_failed"}
    log.info("accept_removal_recorded", extra={**coords, "actor": actor})
    try:
        do_enqueue(install_id=install_id, repo=repo_full, pr_number=pr_number,
                   persona="code_reviewer", dedup_key=f"accept-removal:{comment_id}")
    except Exception as e:  # noqa: BLE001 - answered; the acceptance stands
        log.warning("accept_removal_enqueue_failed", extra={**coords, "kind": type(e).__name__})
        say(f"Grug recorded `{actor}` accepting the removal of `{path}` on "
            f"`{head_sha[:8]}`, but could not queue Elder. Comment `/grug "
            "improve` to re-review.")
        return {**out, "status": "skip", "reason": "enqueue_failed"}
    say(f"Grug recorded `{actor}` accepting the removal of `{path}` on "
        f"`{head_sha[:8]}`. Elder is re-reviewing this PR. A new push needs "
        "a new acceptance. So speaks Grug.")
    return {**out, "status": "dispatched", "persona": "code_reviewer"}
