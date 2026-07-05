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


def _fetch_diff(token: str, owner: str, repo: str, pr_number: int) -> str:
    resp = httpx.get(
        f"{_API}/repos/{_q(owner, safe='')}/{_q(repo, safe='')}/pulls/{pr_number}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3.diff"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def run_command(
    verb: str, arg: str, *, install_id: int, owner: str, repo: str,
    pr_number: int, comment_id: int, token_fn, enqueue=None, claim=None,
) -> dict[str, str]:
    """Execute a gated command. `token_fn` = with_install_token_retry-style
    caller; `enqueue`/`claim` are injectable for tests (default to the real
    rerun + store). Returns an audit dict."""
    if claim is None:
        from adapters.pg_install_store import claim_delivery  # type: ignore
        claim = claim_delivery
    # Idempotency per comment_id: a re-delivered webhook must not double-act.
    if not claim(f"grugcmd:{comment_id}"):
        return {"status": "no_op", "reason": "comment already handled", "verb": verb}

    if verb in ("improve", "test-gaps"):
        persona = "code_reviewer" if verb == "improve" else "smasher"
        if enqueue is None:
            from rerun import enqueue_rerun  # type: ignore
            enqueue = enqueue_rerun
        try:
            enqueue(install_id=install_id, repo=f"{owner}/{repo}", pr_number=pr_number, persona=persona)
        except Exception as e:  # noqa: BLE001 - rerun is best-effort
            log.warning("interactive_enqueue_failed", extra={"verb": verb, "kind": type(e).__name__})
            return {"status": "skip", "reason": "enqueue_failed", "verb": verb}
        note = ("Elder is re-reviewing this PR." if verb == "improve"
                else "Smasher is checking for test gaps.")
        token_fn(lambda t: _post_reply(t, owner, repo, pr_number, f"{note} So speaks Grug."))
        return {"status": "dispatched", "verb": verb, "persona": persona}

    if verb == "ask":
        if not arg:
            token_fn(lambda t: _post_reply(t, owner, repo, pr_number,
                                           "Ask Grug what? Usage: `/grug ask <question>`."))
            return {"status": "no_op", "reason": "empty question", "verb": verb}
        from llm_client import answer_pr_question  # type: ignore

        def _do(token: str) -> None:
            diff = _fetch_diff(token, owner, repo, pr_number)
            answer = answer_pr_question(arg, diff, install_id)
            body = (f"{answer}\n\n*(Grug answered from the PR diff - may be wrong; verify.)*"
                    if answer else
                    "Grug could not answer that right now (the thinking-rock is tired). Try again.")
            _post_reply(token, owner, repo, pr_number, body)
        token_fn(_do)
        return {"status": "dispatched", "verb": verb}

    return {"status": "no_op", "reason": f"unknown verb {verb}", "verb": verb}
