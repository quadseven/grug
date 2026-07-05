"""Interactive `/grug` command actions (#528, epic #522).

Executes a parsed GrugCommand after the dispatcher has GATED it (allowlist,
tpm-enabled, author-or-write-collaborator). All actions are default-safe -
they re-review, answer, or post; none mutate code. Idempotent per
comment_id so a re-delivered webhook doesn't double-act.

- improve   -> enqueue an Elder (code_reviewer) re-run via the rerun lane
- test-gaps -> enqueue a Smasher re-run
- ask <q>   -> LLM Q&A over the PR diff, posted as a reply
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


def run_command(
    verb: str, arg: str, *, install_id: int, owner: str, repo: str,
    pr_number: int, comment_id: int, token_fn, enqueue=None, enqueue_ask=None,
    claim=None,
) -> dict[str, str]:
    """Execute a gated command. All heavy work is ASYNC via the rerun lane -
    the webhook only enqueues (fast) so the delivery ACK never blocks on an
    LLM call (#528 Qodo). `enqueue`/`enqueue_ask`/`claim` are injectable for
    tests. Claim happens AFTER a successful enqueue, so a claim-then-crash
    can't silently swallow the command."""
    if claim is None:
        from adapters.pg_install_store import claim_delivery  # type: ignore
        claim = claim_delivery

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
