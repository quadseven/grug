# WEBHOOK-ONLY (NOT mirrored): the `/grug ask` job the re-run consumer runs.
"""The `/grug ask` job (#528, #695, #822), moved out of rerun.py (#1592).

The webhook enqueues an ask job with `rerun.enqueue_ask`; the consumer's
`rerun._run_one` hands a job of kind `ask` to `run_ask_job` here. The job
shape and the logger are unchanged, so the consumer logs read the same.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx

from github_app_auth import with_install_token_retry
from rerun import _FETCH_TIMEOUT, _GH_API, _comment_react, _comment_unreact, _gh_get, _gh_post

# The rerun logger, kept so the ask lines land where they always have.
log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.rerun")


def _gh_get_text(token: str, url: str, *, accept: str) -> str:
    resp = httpx.get(
        url, headers={"Authorization": f"Bearer {token}", "Accept": accept},
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def _gh_patch(token: str, url: str, json_body: dict[str, Any]) -> None:
    resp = httpx.patch(
        url, json=json_body,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()


def _thread_context(token: str, owner: str, repo_name: str, root_comment_id: int) -> str:
    """The review-thread block for an in-thread ask (#822): the comment the
    thread is about (usually one of Grug's findings) plus its diff hunk, so
    the answer can cite the specific finding and lines. Best-effort - an
    unreadable thread degrades to the plain diff context, never to silence."""
    from urllib.parse import quote as _q
    try:
        c = _gh_get(
            token,
            f"{_GH_API}/repos/{_q(owner, safe='')}/{_q(repo_name, safe='')}"
            f"/pulls/comments/{root_comment_id}",
        )
    except Exception as e:  # noqa: BLE001 - context enrichment must not kill the answer
        log.warning(
            "ask_thread_context_failed",
            extra={"comment_id": root_comment_id, "kind": type(e).__name__},
        )
        return ""
    parts = ["REVIEW THREAD the question was asked in:"]
    path = str(c.get("path") or "")
    line = c.get("line") or c.get("original_line")
    if path:
        parts.append(f"File: {path}" + (f", line {line}" if line else ""))
    if c.get("body"):
        parts.append(f"Comment under discussion:\n{c['body']}")
    if c.get("diff_hunk"):
        parts.append(f"Diff hunk:\n{c['diff_hunk']}")
    return "\n".join(parts) if len(parts) > 1 else ""


# #695: the comment a `/grug ask` answer will land in is posted FIRST, so
# the asker sees Grug heard them, then PATCHed in place to the answer.
_ASK_PLACEHOLDER = (
    "Grug thinking on this. This comment turns into the answer when Grug "
    "is done."
)
# Criterion 6 of #528's wording: the one real message for "no answer".
_ASK_TIRED = "Grug not know. The thinking-rock is tired - try again."


def run_ask(
    install_id: int, repo_full: str, pr_number: int, question: str,
    reply_to_comment_id: int = 0, comment_id: int = 0,
) -> str:
    """Answer a /grug ask question in the consumer (async, #528).

    The comment is one evolving document (#695): an eyes reaction on the
    asking comment and a placeholder go out BEFORE the diff fetch and the
    LLM call, the answer PATCHes that same placeholder, and the eyes are
    swapped for +1 (answered) or -1 (no answer, failure). Records a gauge.

    With `reply_to_comment_id` (#822) the question came from a review thread:
    the placeholder and the answer post IN that thread (the one notification
    GitHub already sends for a thread reply - no extra email), with the
    thread's finding + hunk ahead of the diff as context.

    Once the placeholder is up, nothing after it raises past the job: a
    redelivered job would post a SECOND placeholder and leave the first one
    hanging. A failure resolves the placeholder to the tired message
    instead, so the asker always reads a real answer and can ask again. A
    failure to post the placeholder itself still raises (nothing is on the
    PR yet, so the retry is clean)."""
    from llm_client import _redact_secrets, answer_pr_question  # type: ignore
    from markdown_safety import neutralize_mentions  # type: ignore
    from observability import emit_gauge  # type: ignore
    owner, _, repo_name = repo_full.partition("/")
    q = _redact_secrets(question)
    base = f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo_name, safe='')}"
    in_thread = bool(reply_to_comment_id)
    if in_thread:
        post_url = f"{base}/pulls/{pr_number}/comments/{reply_to_comment_id}/replies"
        edit_base = f"{base}/pulls/comments"
        footer = "*(Grug answered from the thread and the PR diff - may be wrong; verify.)*"
        lead = ""
    else:
        post_url = f"{base}/issues/{pr_number}/comments"
        edit_base = f"{base}/issues/comments"
        footer = "*(Grug answered from the PR diff - may be wrong; verify.)*"
        # A top-level answer names the question it answers (#822 criterion
        # 1). The quote is neutralized like the answer: it is echoed user
        # text posted under Grug's authority.
        lead = f"> {neutralize_mentions(q[:280])}\n\n"

    eyes_id = _comment_react(install_id, base, comment_id, in_thread, "eyes")
    placeholder = with_install_token_retry(
        install_id, lambda t: _gh_post(t, post_url, {"body": lead + _ASK_PLACEHOLDER}),
    )
    placeholder_id = int((placeholder or {}).get("id") or 0)

    def _compose(token: str) -> tuple[str, str]:
        diff = _gh_get_text(
            token, f"{base}/pulls/{pr_number}",
            accept="application/vnd.github.v3.diff",
        )
        context = diff
        thread_note = ""
        if in_thread:
            thread = _thread_context(token, owner, repo_name, reply_to_comment_id)
            if thread:
                context = f"{thread}\n\nFULL PR DIFF:\n{diff}"
            else:
                # Say so in the open: an answer that silently lost its
                # thread context would read as if it had seen the finding.
                thread_note = (
                    "\n\n*(Grug could not load the thread itself, so this "
                    "answer is from the PR diff only.)*"
                )
        answer = answer_pr_question(
            q, context, install_id,
            pr_context={
                "installation_id": install_id,
                "repo": repo_full,
                "pr_number": pr_number,
            },
        )
        # The answer is model-authored over an attacker-influenceable diff,
        # posted under Grug's own installation-token authority - a live
        # @mention in it would notify a real GitHub user as if Grug itself
        # pinged them (#561, same class as Teller's #554 round-4 fix).
        if not answer:
            return lead + _ASK_TIRED, "ask_no_answer"
        safe_answer = neutralize_mentions(answer)
        return f"{lead}{safe_answer}\n\n{footer}{thread_note}", "answered"

    try:
        body, result = with_install_token_retry(install_id, _compose)
    except Exception as e:  # noqa: BLE001 - the placeholder is up; it must resolve, not hang (#695)
        log.error(
            "ask_failed",
            extra={"repo": repo_full, "pr": pr_number, "kind": type(e).__name__},
            exc_info=True,
        )
        body, result = lead + _ASK_TIRED, "ask_failed"

    def _publish(text: str) -> None:
        if placeholder_id:
            with_install_token_retry(
                install_id,
                lambda t: _gh_patch(t, f"{edit_base}/{placeholder_id}", {"body": text}),
            )
        else:
            # GitHub gave back no id to edit: the answer goes out as its
            # own comment rather than not at all.
            with_install_token_retry(
                install_id, lambda t: _gh_post(t, post_url, {"body": text}),
            )

    try:
        _publish(body)
    except (httpx.HTTPError, RuntimeError) as e:
        # A refused answer (an over-long body, a transient 5xx) must not
        # leave "Grug thinking" up: write the short real message instead.
        log.error(
            "ask_publish_failed",
            extra={"repo": repo_full, "pr": pr_number, "kind": type(e).__name__},
            exc_info=True,
        )
        result = "ask_publish_failed"
        try:
            _publish(lead + _ASK_TIRED)
        except (httpx.HTTPError, RuntimeError) as e2:
            log.error(
                "ask_placeholder_stuck",
                extra={"repo": repo_full, "pr": pr_number,
                       "placeholder_id": placeholder_id, "kind": type(e2).__name__},
                exc_info=True,
            )

    _comment_react(
        install_id, base, comment_id, in_thread,
        "+1" if result == "answered" else "-1",
    )
    _comment_unreact(install_id, base, comment_id, in_thread, eyes_id)
    try:
        emit_gauge("grug.interactive.ask", 1)
    except Exception:  # noqa: BLE001
        pass
    log.info("ask_answered", extra={"repo": repo_full, "pr": pr_number, "result": result})
    return result


def run_ask_job(job: dict[str, Any]) -> str:
    """Run one decoded ask job from the rerun queue."""
    return run_ask(
        int(job["install_id"]), str(job["repo"]), int(job["pr_number"]),
        str(job.get("question", "")),
        reply_to_comment_id=int(job.get("reply_to_comment_id", 0) or 0),
        comment_id=int(job.get("comment_id", 0) or 0),
    )
