"""Interactive `/grug` command actions (#528, epic #522).

Executes a parsed GrugCommand after the dispatcher has GATED it (allowlist,
tpm-enabled, author-or-write-collaborator). All actions are default-safe -
they re-review, answer, or post; none mutate code. Idempotent per
comment_id so a re-delivered webhook doesn't double-act.

- improve   -> enqueue an Elder (code_reviewer) re-run via the rerun lane
- test-gaps -> enqueue a Smasher re-run
- ask <q>   -> LLM Q&A over the PR diff, posted as a reply
- resume    -> lift Elder's auto-pause and enqueue an Elder re-run (grug#1188)
- review    -> enqueue one Elder re-run; an auto-pause stays
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote as _q

import httpx  # type: ignore

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.interactive")

_API = "https://api.github.com"
_TIMEOUT = 15.0


def _post_reply(token: str, owner: str, repo: str, pr_number: int, body: str) -> None:
    httpx.post(
        f"{_API}/repos/{_q(owner, safe='')}/{_q(repo, safe='')}/issues/{pr_number}/comments",
        json={"body": body},
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        timeout=_TIMEOUT,
    ).raise_for_status()


def _pause_after_wake(
    verb: str, coords: dict, resume, is_paused,
) -> tuple[str, str]:
    """Apply `resume`/`review` to Elder's auto-pause. Returns the pause
    state after the command ("true"/"false"/"unknown") and the reply."""
    if resume is None or is_paused is None:
        from adapters.pg_install_store import (  # type: ignore
            is_elder_paused, resume_elder,
        )
        resume = resume or resume_elder
        is_paused = is_paused or is_elder_paused
    if verb == "resume":
        try:
            was_paused = resume(**coords)
        except Exception as e:  # noqa: BLE001 - the review is enqueued; say the pause may hold
            log.warning("interactive_resume_failed", extra={"kind": type(e).__name__})
            return "unknown", ("Elder is reviewing this PR, but Grug could not lift "
                               "the pause. Comment `/grug resume` again.")
        if was_paused:
            return "false", "Elder awake again. Elder review all that changed since the last look."
        return "false", "Elder was not resting. Elder is reviewing this PR."
    try:
        paused = is_paused(**coords)
    except Exception as e:  # noqa: BLE001 - only the reply wording depends on it
        log.warning("interactive_pause_lookup_failed", extra={"kind": type(e).__name__})
        return "unknown", "Elder is reviewing this PR."
    if paused:
        return "true", ("Elder take one look now, then keep resting. "
                        "Comment `/grug resume` to review every push again.")
    return "false", "Elder is reviewing this PR."


def _wake_elder(
    verb: str, *, install_id: int, owner: str, repo: str, pr_number: int,
    comment_id: int, token_fn, enqueue, claim, resume, is_paused,
) -> dict[str, str]:
    """`/grug resume` and `/grug review` (grug#1188): both enqueue an Elder
    review of the current head, which the consumer scopes to the delta since
    the last reviewed head. `resume` also lifts the auto-pause; `review`
    leaves it. The pause is touched only after the enqueue and the claim
    succeed, so a failed enqueue leaves the PR exactly as it was."""
    if enqueue is None:
        from rerun import enqueue_rerun  # type: ignore
        enqueue = enqueue_rerun
    coords = {"install_id": install_id, "repo": f"{owner}/{repo}", "pr_number": pr_number}
    try:
        enqueue(**coords, persona="code_reviewer")
    except Exception as e:  # noqa: BLE001 - enqueue best-effort; NOT claimed on failure
        log.warning("interactive_enqueue_failed", extra={"verb": verb, "kind": type(e).__name__})
        return {"status": "skip", "reason": "enqueue_failed", "verb": verb}
    if not claim(f"grugcmd:{comment_id}"):
        return {"status": "no_op", "reason": "comment already handled", "verb": verb}
    paused, note = _pause_after_wake(verb, coords, resume, is_paused)
    try:
        token_fn(lambda t: _post_reply(t, owner, repo, pr_number, f"{note} So speaks Grug."))
    except Exception as e:  # noqa: BLE001 - the review is enqueued; the ack reply is cosmetic
        log.warning("interactive_ack_failed", extra={"verb": verb, "kind": type(e).__name__})
    return {"status": "dispatched", "verb": verb, "persona": "code_reviewer", "paused": paused}


def run_command(
    verb: str, arg: str, *, install_id: int, owner: str, repo: str,
    pr_number: int, comment_id: int, token_fn, enqueue=None, enqueue_ask=None,
    claim=None, resume=None, is_paused=None,
) -> dict[str, str]:
    """Execute a gated command. All heavy work is ASYNC via the rerun lane -
    the webhook only enqueues (fast) so the delivery ACK never blocks on an
    LLM call (#528 LORE). `enqueue`/`enqueue_ask`/`claim`/`resume`/
    `is_paused` are injectable for tests. Claim happens AFTER a successful
    enqueue, so a claim-then-crash can't silently swallow the command."""
    if claim is None:
        from adapters.pg_install_store import claim_delivery  # type: ignore
        claim = claim_delivery

    if verb in ("resume", "review"):
        return _wake_elder(
            verb, install_id=install_id, owner=owner, repo=repo,
            pr_number=pr_number, comment_id=comment_id, token_fn=token_fn,
            enqueue=enqueue, claim=claim, resume=resume, is_paused=is_paused,
        )

    repo_full = f"{owner}/{repo}"
    if verb in ("improve", "test-gaps"):
        persona = "code_reviewer" if verb == "improve" else "smasher"
        if enqueue is None:
            from rerun import enqueue_rerun  # type: ignore
            enqueue = enqueue_rerun
        try:
            enqueue(install_id=install_id, repo=repo_full, pr_number=pr_number, persona=persona)
        except Exception as e:  # noqa: BLE001 - enqueue best-effort; NOT claimed on failure
            log.warning("interactive_enqueue_failed", extra={"verb": verb, "kind": type(e).__name__})
            return {"status": "skip", "reason": "enqueue_failed", "verb": verb}
        if not claim(f"grugcmd:{comment_id}"):
            return {"status": "no_op", "reason": "comment already handled", "verb": verb}
        note = ("Elder is re-reviewing this PR." if verb == "improve"
                else "Smasher is checking for test gaps.")
        try:
            token_fn(lambda t: _post_reply(t, owner, repo, pr_number, f"{note} So speaks Grug."))
        except Exception as e:  # noqa: BLE001 - the rerun is enqueued; the ack reply is cosmetic
            log.warning("interactive_ack_failed", extra={"verb": verb, "kind": type(e).__name__})
        return {"status": "dispatched", "verb": verb, "persona": persona}

    if verb == "ask":
        if not arg:
            try:
                token_fn(lambda t: _post_reply(t, owner, repo, pr_number,
                                               "Ask Grug what? Usage: `/grug ask <question>`."))
            except Exception:  # noqa: BLE001
                pass
            return {"status": "no_op", "reason": "empty question", "verb": verb}
        if enqueue_ask is None:
            from rerun import enqueue_ask as _ea  # type: ignore
            enqueue_ask = _ea
        try:
            # The heavy LLM Q&A runs in the CONSUMER (async), not here - the
            # webhook must ACK fast. The consumer redacts + answers + replies.
            enqueue_ask(install_id=install_id, repo=repo_full, pr_number=pr_number,
                        comment_id=comment_id, question=arg)
        except Exception as e:  # noqa: BLE001
            log.warning("interactive_ask_enqueue_failed", extra={"kind": type(e).__name__})
            return {"status": "skip", "reason": "enqueue_failed", "verb": verb}
        if not claim(f"grugcmd:{comment_id}"):
            return {"status": "no_op", "reason": "comment already handled", "verb": verb}
        return {"status": "dispatched", "verb": verb}

    return {"status": "no_op", "reason": f"unknown verb {verb}", "verb": verb}
