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
- learnings list  -> list the repo's remembered markings (read-only reply)
- learnings delete <digest>  -> forget one remembered marking by digest
- accept-removal <path>  -> record a trusted maintainer accepting one deleted
  guard path on the current head, then re-run Elder (grug#1405)

grug#1270 - an Elder re-run now (a) is refused while one is already running
on the same head, (b) puts the `grug:reviewing` label on the PR while it
runs, and (c) is KEPT (deferred to the GitHub budget's reset) instead of
being dropped when the installation's hourly budget is exhausted.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote as _q

import httpx  # type: ignore

from adapters.pg_install_store import _learning_digest  # type: ignore

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


def _escape_html(s: str) -> str:
    """Escape HTML-significant chars in a displayed learning so a taught
    rule can't break out of the comment markup or inject tags."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _reply(token_fn, owner: str, repo: str, pr_number: int, body: str) -> bool:
    """Post one reply. True when the answer landed.

    Best-effort: the command's effect (the enqueue) already happened, so a
    reply GitHub refuses must not raise out of the command. The caller is
    told whether the answer landed, though - a command that was heard and
    never answered must stay answerable (grug#1270)."""
    try:
        token_fn(lambda t: _post_reply(t, owner, repo, pr_number, body))
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning("interactive_reply_failed", extra={"kind": type(e).__name__})
        return False
    return True


def _elder_review_state(*, token_fn, owner: str, repo: str, head_sha: str) -> str:
    """"running" | "idle" | "unknown" - is Elder reviewing this head right now?

    The Elder CHECK RUN is the source of truth - the same ground truth
    `rerun._elder_check_already_terminal_or_pending` reads. Without this a
    second `/grug improve` queues a SECOND review of the same commit, which
    is how one burst of 14 PRs spent an hour of the installation's GitHub
    budget on 2026-10-18 (grug#1270).

    "unknown" is a real answer, not a failure: the check-run listing could
    not be read. It must not be read as "idle" (that would queue a second
    review of a commit Elder is already on), and it must not be reported as
    "Elder is reviewing" either (it may not be)."""
    if not head_sha:
        # An empty head is not a review that finished: it is a head nobody
        # knows. "idle" would read it as "Elder is not reviewing" and let a
        # second review queue on nothing (grug#1270).
        return "unknown"
    from review_label import elder_check_state  # type: ignore

    try:
        state = token_fn(
            lambda tok: elder_check_state(
                token=tok, owner=owner, repo=repo, head_sha=head_sha,
            )
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "interactive_elder_state_failed",
            extra={"kind": type(e).__name__, "head_sha": head_sha[:8]},
        )
        return "unknown"
    if state == "running":
        return "running"
    if state == "unknown":
        # `elder_check_state` answers "unknown" when GitHub could not be
        # read. That is NOT "idle": queueing a second review of a commit
        # Elder may already be on is the overlap this ticket exists to stop.
        return "unknown"
    return "idle"


def _mark_reviewing(*, token_fn, owner: str, repo: str, pr_number: int) -> bool:
    """Add the `grug:reviewing` label - the visible sign that Elder is
    re-reviewing this PR.

    Best-effort by design: the CHECK RUN is the truth and the label is only
    the sign, so a write GitHub refuses must never fail the command (the
    consumer's sweep clears the label when no review is running). True when
    the PR carries the label afterwards - a refused write is logged as an
    error, because a missing sign and a raised sign must not look alike."""
    from review_label import add_reviewing_label  # type: ignore

    try:
        attached = bool(token_fn(
            lambda tok: add_reviewing_label(
                token=tok, owner=owner, repo=repo, pr_number=pr_number,
            )
        ))
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "interactive_reviewing_label_failed",
            extra={"kind": type(e).__name__, "pr": pr_number},
        )
        return False
    if not attached:
        log.error(
            "interactive_reviewing_label_refused",
            extra={"pr": pr_number, "repo": f"{owner}/{repo}"},
        )
    return attached


def _defer_elder_review(
    *, verb: str, install_id: int, owner: str, repo: str, pr_number: int,
    comment_id: int, token_fn, enqueue, claim, defer_until: float,
) -> dict[str, str]:
    """Keep an Elder re-run instead of dropping it.

    The installation's hourly GitHub budget was exhausted, so the review
    cannot run yet. The job goes back on the queue with a `not_before` at
    the budget's reset, and the reply says so: a command dropped with no
    reply looks exactly like a command that worked and did nothing, which
    is how the 2026-10-18 burst lost a dozen re-runs (grug#1270).

    The CLAIM is taken BEFORE the job is queued. A deferred job that sits
    on the queue is a review that WILL run later, so a redelivered comment
    must not queue the same command a second time - two Elder reviews of
    one head is the overlap this ticket exists to stop. A command that
    cannot even be queued is ANSWERED (the reader is told to comment
    again), so it is never dropped without a trace."""
    repo_full = f"{owner}/{repo}"
    if not claim(f"grugcmd:{comment_id}"):
        return {"status": "no_op", "reason": "comment already handled", "verb": verb}
    try:
        enqueue(
            install_id=install_id, repo=repo_full, pr_number=pr_number,
            persona="code_reviewer", not_before=float(defer_until),
        )
    except Exception as e:  # noqa: BLE001 - the queue is the keep-path
        log.error(
            "interactive_defer_enqueue_failed",
            extra={"verb": verb, "kind": type(e).__name__},
            exc_info=True,
        )
        _reply(
            token_fn, owner, repo, pr_number,
            "Grug could not queue the deferred review. Comment "
            "`/grug improve` again.",
        )
        return {"status": "skip", "reason": "defer_enqueue_failed", "verb": verb}
    log.warning(
        "interactive_elder_rerun_deferred",
        extra={
            "verb": verb,
            "repo": repo_full,
            "pr": pr_number,
            "defer_until": int(defer_until),
        },
    )
    _reply(
        token_fn, owner, repo, pr_number,
        "Grug's GitHub budget is spent. Elder will re-review this PR when "
        "the budget resets.",
    )
    return {"status": "deferred", "reason": "github_rate_limited", "verb": verb}


def _already_reviewing(
    *, verb: str, owner: str, repo: str, pr_number: int, comment_id: int,
    token_fn, claim, head_sha: str, state: str = "running",
) -> dict[str, str]:
    """Answer a second `/grug improve` instead of queueing a second review.

    The reply names the head Elder is already on, so the reader can see
    that the command was heard and deliberately not queued - a silent
    no-op is what made people fire the command again (grug#1270). An
    unreadable check-run listing says exactly that, instead of claiming a
    review that may not be running.

    The answer is posted BEFORE the claim is taken. A claim taken first
    turns a failed reply into a command that was heard and never answered,
    which is the silent drop this ticket exists to stop: the command stays
    answerable until the answer has actually landed."""
    log.info(
        "interactive_elder_rerun_skipped_already_running",
        extra={
            "verb": verb,
            "repo": f"{owner}/{repo}",
            "pr": pr_number,
            "head_sha": head_sha[:8],
            "state": state,
        },
    )
    body = (
        f"Elder is already reviewing {head_sha[:8]}."
        if state == "running"
        else (
            "Grug could not tell whether Elder is already reviewing this "
            "PR, so the re-review was not queued. Comment `/grug improve` "
            "again."
        )
    )
    if not _reply(token_fn, owner, repo, pr_number, body):
        # The answer did not land: leave the command unclaimed so a
        # redelivery can answer it.
        return {"status": "skip", "reason": "reply_failed", "verb": verb}
    if not claim(f"grugcmd:{comment_id}"):
        return {"status": "no_op", "reason": "comment already handled", "verb": verb}
    return {"status": "no_op", "reason": "already_reviewing", "verb": verb}


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
    head_sha: str = "", defer_until: float = 0.0,
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
    if defer_until:
        return _defer_elder_review(
            verb=verb, install_id=install_id, owner=owner, repo=repo,
            pr_number=pr_number, comment_id=comment_id, token_fn=token_fn,
            enqueue=enqueue, claim=claim, defer_until=defer_until,
        )
    elder_state = _elder_review_state(
        token_fn=token_fn, owner=owner, repo=repo, head_sha=head_sha,
    )
    if elder_state != "idle":
        return _already_reviewing(
            verb=verb, owner=owner, repo=repo, pr_number=pr_number,
            comment_id=comment_id, token_fn=token_fn, claim=claim,
            head_sha=head_sha, state=elder_state,
        )
    try:
        enqueue(**coords, persona="code_reviewer")
    except Exception as e:  # noqa: BLE001 - enqueue best-effort; NOT claimed on failure
        log.warning("interactive_enqueue_failed", extra={"verb": verb, "kind": type(e).__name__})
        return {"status": "skip", "reason": "enqueue_failed", "verb": verb}
    if not claim(f"grugcmd:{comment_id}"):
        return {"status": "no_op", "reason": "comment already handled", "verb": verb}
    # The sign goes up only on the path that owns the command: a comment
    # that was already handled must not leave a `grug:reviewing` label
    # behind for a review this delivery did not start (grug#1270).
    _mark_reviewing(
        token_fn=token_fn, owner=owner, repo=repo, pr_number=pr_number,
    )
    paused, note = _pause_after_wake(verb, coords, resume, is_paused)
    try:
        token_fn(lambda t: _post_reply(t, owner, repo, pr_number, f"{note} So speaks Grug."))
    except Exception as e:  # noqa: BLE001 - the review is enqueued; the ack reply is cosmetic
        log.warning("interactive_ack_failed", extra={"verb": verb, "kind": type(e).__name__})
    return {"status": "dispatched", "verb": verb, "persona": "code_reviewer", "paused": paused}


def _run_persona_rerun(
    verb: str, *, install_id: int, owner: str, repo: str, pr_number: int,
    comment_id: int, token_fn, enqueue, claim, head_sha: str = "",
    defer_until: float = 0.0,
) -> dict[str, str]:
    """`/grug improve` (Elder) and `/grug test-gaps` (Smasher): enqueue the
    persona's re-run, label it (Elder only), and answer.

    Elder's re-run is the one that must not overlap, must carry the
    `grug:reviewing` label, and must be kept when the installation's GitHub
    budget is spent (grug#1270)."""
    persona = "code_reviewer" if verb == "improve" else "smasher"
    if enqueue is None:
        from rerun import enqueue_rerun  # type: ignore
        enqueue = enqueue_rerun
    repo_full = f"{owner}/{repo}"
    if verb == "improve":
        if defer_until:
            return _defer_elder_review(
                verb=verb, install_id=install_id, owner=owner, repo=repo,
                pr_number=pr_number, comment_id=comment_id, token_fn=token_fn,
                enqueue=enqueue, claim=claim, defer_until=defer_until,
            )
        elder_state = _elder_review_state(
            token_fn=token_fn, owner=owner, repo=repo, head_sha=head_sha,
        )
        if elder_state != "idle":
            return _already_reviewing(
                verb=verb, owner=owner, repo=repo, pr_number=pr_number,
                comment_id=comment_id, token_fn=token_fn, claim=claim,
                head_sha=head_sha, state=elder_state,
            )
    try:
        enqueue(install_id=install_id, repo=repo_full, pr_number=pr_number, persona=persona)
    except Exception as e:  # noqa: BLE001 - enqueue best-effort; NOT claimed on failure
        log.warning("interactive_enqueue_failed", extra={"verb": verb, "kind": type(e).__name__})
        return {"status": "skip", "reason": "enqueue_failed", "verb": verb}
    if not claim(f"grugcmd:{comment_id}"):
        return {"status": "no_op", "reason": "comment already handled", "verb": verb}
    if verb == "improve":
        # The sign goes up only on the path that owns the command: a
        # comment that was already handled must not leave a
        # `grug:reviewing` label behind for a review this delivery did
        # not start (grug#1270).
        _mark_reviewing(
            token_fn=token_fn, owner=owner, repo=repo, pr_number=pr_number,
        )
    note = ("Elder is re-reviewing this PR." if verb == "improve"
            else "Smasher is checking for test gaps.")
    try:
        token_fn(lambda t: _post_reply(t, owner, repo, pr_number, f"{note} So speaks Grug."))
    except Exception as e:  # noqa: BLE001 - the rerun is enqueued; the ack reply is cosmetic
        log.warning("interactive_ack_failed", extra={"verb": verb, "kind": type(e).__name__})
    return {"status": "dispatched", "verb": verb, "persona": persona}


def run_command(
    verb: str, arg: str, *, install_id: int, owner: str, repo: str,
    pr_number: int, comment_id: int, token_fn, enqueue=None, enqueue_ask=None,
    claim=None, resume=None, is_paused=None, head_sha: str = "",
    defer_until: float = 0.0, actor: str = "",
) -> dict[str, str]:
    """Execute a gated command. All heavy work is ASYNC via the rerun lane -
    the webhook only enqueues (fast) so the delivery ACK never blocks on an
    LLM call (#528 LORE). `enqueue`/`enqueue_ask`/`claim`/`resume`/
    `is_paused` are injectable for tests. Claim happens AFTER a successful
    enqueue, so a claim-then-crash can't silently swallow the command.

    The DEFERRED path is the one exception: there the enqueue is a KEEP (a
    job that will run later, not the work itself), so the claim comes
    first - a redelivered comment must not queue the same command twice.
    A command that cannot even be queued is answered instead, so it is
    never dropped without a trace (grug#1270).

    `head_sha` is the PR's current head - the ground truth for "is Elder
    already reviewing this?". `defer_until` is the epoch second the
    installation's GitHub budget resets at: a non-zero value keeps the
    command instead of dropping it (grug#1270). `actor` is the login that
    passed `accept-removal`'s stricter trust gate (grug#1405)."""
    if verb == "accept-removal":
        from accept_removal import run_accept_removal  # type: ignore
        return run_accept_removal(
            arg, install_id=install_id, owner=owner, repo=repo,
            pr_number=pr_number, comment_id=comment_id, head_sha=head_sha,
            actor=actor, token_fn=token_fn, enqueue=enqueue, claim=claim,
        )

    if claim is None:
        from adapters.pg_install_store import claim_delivery  # type: ignore
        claim = claim_delivery

    if verb in ("resume", "review"):
        return _wake_elder(
            verb, install_id=install_id, owner=owner, repo=repo,
            pr_number=pr_number, comment_id=comment_id, token_fn=token_fn,
            enqueue=enqueue, claim=claim, resume=resume, is_paused=is_paused,
            head_sha=head_sha, defer_until=defer_until,
        )

    repo_full = f"{owner}/{repo}"
    if verb in ("improve", "test-gaps"):
        return _run_persona_rerun(
            verb, install_id=install_id, owner=owner, repo=repo,
            pr_number=pr_number, comment_id=comment_id, token_fn=token_fn,
            enqueue=enqueue, claim=claim, head_sha=head_sha,
            defer_until=defer_until,
        )

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

    if verb == "learnings":
        parts = arg.split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "list":
            try:
                rows = _wake_learnings_list(repo_full)
            except Exception as e:  # noqa: BLE001
                log.warning("interactive_learnings_list_failed",
                            extra={"kind": type(e).__name__})
                rows = []
            lines = [f"Grug's remembered markings for `{repo_full}` (active, newest first):"]
            for row in reversed(rows):
                text = str(row.get("text", "")).strip()
                digest = _learning_digest(text)
                scope = str(row.get("scope_path", "")).strip()
                scope_txt = f", scoped to `{_escape_html(scope)}`" if scope else ""
                lines.append(
                    f"- `{digest}`: {_escape_html(text)}{scope_txt} | "
                    f"taught by `{_escape_html(str(row.get('author', '')))}` on PR #{row.get('source_pr', 0)} | "
                    f"used {int(row.get('usage_count', 0) or 0)}"
                )
            if not rows:
                lines.append("(none)")
            lines.append("")
            lines.append("Use `/grug learnings delete <digest>` to forget one.")
            body = "\n".join(lines)
            try:
                token_fn(lambda t: _post_reply(t, owner, repo, pr_number, body))
            except Exception:  # noqa: BLE001 - the list is read-only; a failed post is cosmetic
                log.warning("interactive_learnings_list_post_failed")
            return {"status": "dispatched", "verb": "learnings", "sub": "list"}

        if sub == "delete":
            if not rest:
                try:
                    token_fn(lambda t: _post_reply(t, owner, repo, pr_number,
                                                   "Usage: `/grug learnings delete <digest>`."))
                except Exception:  # noqa: BLE001, S110 - cosmetic reply; a failed post must not fail the command
                    pass
                return {"status": "no_op", "reason": "missing digest", "verb": verb}
            digest = rest.strip().strip("`")
            removed = False
            if claim(f"learnings:{comment_id}"):
                try:
                    removed = _wake_learnings_delete(repo_full, digest)
                except Exception as e:  # noqa: BLE001
                    log.warning("interactive_learnings_delete_failed",
                                extra={"digest": digest, "kind": type(e).__name__})
            if removed:
                body = f"Forgot marking `{digest}` on `{repo_full}`."
            else:
                body = f"No such marking `{digest}` on `{repo_full}`."
            try:
                token_fn(lambda t: _post_reply(t, owner, repo, pr_number, body))
            except Exception:  # noqa: BLE001, S110 - cosmetic reply; a failed post must not fail the command
                pass
            return {"status": "dispatched", "verb": "learnings", "sub": "delete"}

        try:
            token_fn(lambda t: _post_reply(t, owner, repo, pr_number,
                                           "Usage: `/grug learnings list` or `/grug learnings delete <digest>`."))
        except Exception:  # noqa: BLE001, S110 - cosmetic reply; a failed post must not fail the command
            pass
        return {"status": "no_op", "reason": "unknown learnings sub-command", "verb": verb}

    return {"status": "no_op", "reason": f"unknown verb {verb}", "verb": verb}


def _wake_learnings_list(repo: str) -> list[dict]:
    """Fetch the repo's active learnings (store seam - monkeypatched in tests)."""
    from adapters.pg_install_store import list_learnings  # type: ignore
    return list_learnings(repo)


def _wake_learnings_delete(repo: str, digest: str) -> bool:
    """Forget one learning by digest (store seam - monkeypatched in tests)."""
    from adapters.pg_install_store import delete_learning  # type: ignore
    return delete_learning(repo, digest)
