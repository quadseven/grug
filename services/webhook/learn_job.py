# WEBHOOK-ONLY (NOT mirrored): the learn job the re-run consumer runs.
"""The learn job (#670, ADR-0020), moved out of rerun.py (#1592).

The webhook enqueues a learn job with `rerun.enqueue_learn` when a person
or agent replies to one of Grug's findings; the consumer's `rerun._run_one`
hands a job of kind `learn` to `run_learn_job` here. The job shape and the
logger are unchanged, so the consumer logs read the same.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import quote

import httpx

from github_app_auth import with_install_token_retry
from personas.code_reviewer.living_review import sync_pr as _sync_living_review
from rerun import _GH_API, _gh_post, enqueue_learn
from rerun_queue import JobNotDue

# The rerun logger, kept so the learn lines land where they always have.
log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.rerun")

# A learn job whose classifier backends are all rate limited or down (the
# OpenRouter key is free-tier only and refuses intermittently - operator
# decision 2026-09-23) is DEFERRED: re-enqueued as a fresh message with a
# `not_before`, so the wait never counts toward the rerun DLQ's
# maxReceiveCount. Backoff doubles from 15m to a 6h cap (one SQS visibility
# hide covers it; SQS allows 12h). Past 7 days the job completes with the
# in-thread "reply again later" notice, so a reply is never dead-lettered
# and never silently lost.
_LEARN_DEFER_BASE_SECONDS = 900
_LEARN_DEFER_MAX_SECONDS = 6 * 3600
_LEARN_DEFER_MAX_AGE_SECONDS = 7 * 86400


def _now() -> float:
    """Wall clock, as a seam tests can pin."""
    return time.time()


def _learn_defer_delay(defer_count: int) -> int:
    """Seconds to wait before the next classify attempt of a deferred job."""
    return min(
        _LEARN_DEFER_MAX_SECONDS,
        _LEARN_DEFER_BASE_SECONDS * 2 ** min(max(defer_count, 0), 16),
    )


_DEFUSE_MD_CAP = 600


def _defuse_md(s: str) -> str:
    """Neutralize model-produced text before it lands in a GitHub comment
    body. The learning/scope come from the classifier (derived from an
    untrusted reply), so strip HTML-significant chars (a stray </details>
    would break out of the ack's collapsible), backticks/pipes, and flatten
    newlines; cap to bound a hostile payload.

    Logs when the cap actually truncates (PR #679 Elder finding): the raw
    length is captured, not the defused one, since the HTML-escaping above
    can only grow the string - measuring pre-escape avoids under-counting
    a truncation the escaping itself pushed past the cap."""
    defused = (
        s.replace("<", "&lt;").replace(">", "&gt;")
        .replace("`", "'").replace("|", "\\|")
        .replace("\r", " ").replace("\n", " ")
        # A zero-width space after @ breaks GitHub's mention parsing, so a
        # model-restated "@org/team" cannot ping people from grug's ack
        # (renders identically to the reader).
        .replace("@", "@\u200b")
    )
    if len(defused) > _DEFUSE_MD_CAP:
        log.info("defuse_md_truncated", extra={"raw_len": len(s), "cap": _DEFUSE_MD_CAP})
    return defused[:_DEFUSE_MD_CAP]


# Every classifier backend is refusing for a config/billing reason. The reply
# was NOT judged; saying so (instead of silence) is the maintainer's cue that a
# re-reply later is how it gets remembered.
_LEARN_UNUSABLE: Any = object()  # sentinel: classifier refused, not a verdict
# sentinel: backends stayed rate limited or down past the defer horizon
_LEARN_DEFER_EXHAUSTED: Any = object()


# A reply to a finding is answered with a REACTION, never a comment: replies
# are mostly agents talking to grug, and every grug comment is an email to a
# person who is not reading them (operator, 2026-10-05). A reaction is visible
# to the agent and the human, and notifies no one.
LEARN_REACTION_LEARNED = "+1"      # stored as a lasting rule
LEARN_REACTION_ONE_OFF = "eyes"    # read, applies to this PR only
LEARN_REACTION_UNJUDGED = "confused"  # classifier down; reply again to retry


def _react_to_learn_reply(
    install_id: int, owner: str, repo_name: str, comment_id: int, content: str,
) -> None:
    """React to the person's (or agent's) reply comment. Raises on failure;
    each caller decides whether that failure is worth a redrive. GitHub
    de-duplicates a repeated reaction, so a redelivery is harmless."""
    from urllib.parse import quote as _q

    def _react(token: str) -> None:
        _gh_post(
            token,
            f"{_GH_API}/repos/{_q(owner, safe='')}/{_q(repo_name, safe='')}"
            f"/pulls/comments/{comment_id}/reactions",
            {"content": content},
        )
    with_install_token_retry(install_id, _react)


def _defer_learn(
    install_id: int, repo_full: str, pr_number: int,
    comment_id: int, parent_comment_id: int, reply_text: str, author: str,
    defer_count: int, first_deferred_at: float, statuses: tuple[str, ...],
) -> bool:
    """Re-enqueue a learn job whose classifier backends are all rate limited
    or down. Returns False when the job is past the defer horizon instead.

    Raises if the send fails, so the CURRENT message redrives rather than
    completing with no copy left anywhere."""
    now = _now()
    started = first_deferred_at or now
    if now - started >= _LEARN_DEFER_MAX_AGE_SECONDS:
        log.warning("learn_classifier_defer_exhausted", extra={
            "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
            "defer_count": defer_count, "statuses": list(statuses)})
        return False
    delay = _learn_defer_delay(defer_count)
    enqueue_learn(
        install_id=install_id, repo=repo_full, pr_number=pr_number,
        comment_id=comment_id, parent_comment_id=parent_comment_id,
        reply_text=reply_text, author=author,
        defer_count=defer_count + 1, not_before=now + delay,
        first_deferred_at=started,
    )
    # Warning, not error: on this deployment the free-tier key refusing is
    # expected, and nothing pages on this line.
    log.warning("learn_classifier_deferred", extra={
        "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
        "defer_count": defer_count + 1, "delay_s": delay,
        "statuses": list(statuses)})
    return True


def _store_learning(
    install_id: int, repo_full: str, pr_number: int, comment_id: int,
    author: str, reply_text: str, record: dict, classification: dict,
    *, stored: bool,
) -> tuple[str, str, bool]:
    """Store a durable verdict and return `(ack, result, refused)`.

    A rule that touches a security or SAST class is refused before the
    store, so no later edit or status change can arm it; select_for_prompt
    checks again at read time for rows that predate #1571. `stored` marks a
    redelivery whose rule is already kept: it stores nothing, since a
    re-stamp would restart the 90 days with no human act behind it."""
    from adapters.install_store import put_learning  # type: ignore
    from cave_drawings import MAX_TEXT_CHARS, touches_protected  # type: ignore
    from redact import redact_secrets  # type: ignore

    if not classification["durable"]:
        # Deliberate one-off: acknowledge without storing.
        return LEARN_REACTION_ONE_OFF, "learn_one_off", False
    finding_tags = dict(record.get("finding_tags", {}))
    finding_class = str(finding_tags.get("rule_name", ""))
    finding_category = str(finding_tags.get("category", ""))
    if touches_protected({
        "text": classification["learning"],
        "finding_class": finding_class, "finding_category": finding_category,
    }):
        return LEARN_REACTION_ONE_OFF, "learn_refused_protected", True
    if not stored:
        put_learning(
            repo=repo_full,
            text=classification["learning"],
            scope_path=classification["scope_path"],
            source_pr=pr_number,
            source_comment_id=comment_id,
            author=author,  # the maintainer who TAUGHT it (reply sender)
            approval_delay_seconds=_org_learning_approval_delay(install_id, repo_full),
            reason=redact_secrets(reply_text)[:MAX_TEXT_CHARS],
            finding_class=finding_class,
            finding_category=finding_category,
        )
    return LEARN_REACTION_LEARNED, "learned", False


def _run_learn(
    install_id: int, repo_full: str, pr_number: int,
    comment_id: int, parent_comment_id: int, reply_text: str,
    author: str = "", *, defer_count: int = 0, first_deferred_at: float = 0.0,
) -> str:
    """Classify a maintainer's reply to a finding and, if it is a durable team
    preference, store it and acknowledge in the thread (#670, ADR-0020).

    Backend failures split three ways. Every backend rate limited, down or
    unreachable (the free-tier OpenRouter key refuses intermittently) DEFERS
    the job: a delayed copy is enqueued and this message completes, so the
    wait never counts toward the DLQ. Every backend answering 401/402/403/404
    for a config reason completes the job with a notice in the thread, since
    no retry clears it. Answers that do not parse raise for SQS redrive (a
    miss must not be mislabeled a deliberate one-off, and no ack is posted so
    the retry can succeed). A definite verdict (durable OR one-off) is
    win-once per reply comment: the first run acks, a redelivery is a no-op,
    so the finding thread never gets duplicate acknowledgments."""
    from adapters.install_store import (  # type: ignore
        claim_delivery, get_comment_record, get_learning_by_source_comment,
    )
    from llm_client import (  # type: ignore
        LearnClassifierUnavailable, LearnClassifierUnusable, classify_learning,
    )
    from observability import emit_gauge  # type: ignore

    owner, _, repo_name = repo_full.partition("/")
    record = get_comment_record(install_id, parent_comment_id)
    if record is None:
        # The reply is not to one of grug's tracked findings (or the record
        # TTL-expired). Nothing to learn from; do not post noise.
        log.info("learn_no_parent_record", extra={
            "repo": repo_full, "pr": pr_number, "parent": parent_comment_id})
        return "learn_no_parent"

    # Redelivery fast-path: if THIS reply already produced a stored learning,
    # skip the non-deterministic classifier (a re-run could word the rule
    # differently and store a second row) and reuse the stored rule; the
    # win-once claim below still gates the ack. A transient failure BEFORE
    # the store leaves no row, so the retry classifies cleanly.
    already = get_learning_by_source_comment(repo_full, comment_id)
    if already is not None:
        classification: Any = {
            "durable": True,
            "learning": str(already.get("text", "")),
            "scope_path": str(already.get("scope_path", "")),
        }
    else:
        finding_text = str(record.get("finding_text", ""))
        finding_tags = dict(record.get("finding_tags", {}))
        try:
            classification = classify_learning(
                reply_text, finding_text, finding_tags, install_id,
                pr_context={
                    "installation_id": install_id, "repo": repo_full,
                    "pr_number": pr_number,
                },
            )
        except LearnClassifierUnusable as e:
            # Every backend refused for a config/billing reason (a dead or
            # over-limit key). No redrive clears that, so retrying only walks
            # the job into the rerun DLQ. Complete instead, and tell the
            # maintainer in the thread: re-replying after the fix is the
            # replay path (a new reply is a new job), so the reply is never
            # dropped silently. Operators get llm_backend_unusable plus this
            # line, which names the reply to replay.
            log.error("learn_classifier_unusable", extra={
                "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
                "statuses": list(e.statuses)})
            classification = _LEARN_UNUSABLE
        except LearnClassifierUnavailable as e:
            if _defer_learn(
                install_id, repo_full, pr_number, comment_id,
                parent_comment_id, reply_text, author,
                defer_count, first_deferred_at, e.statuses,
            ):
                return "learn_deferred"
            classification = _LEARN_DEFER_EXHAUSTED
    if classification is None:
        # Transient: backend down or unparseable. Raise for redrive rather
        # than tell the maintainer their durable rule was judged one-off.
        # No store, no claim, no ack, so the retry re-classifies cleanly.
        raise RuntimeError("learn classifier unavailable")

    # STORE FIRST, before the win-once claim. put_learning is idempotent (keyed
    # on the rule digest), so a redelivery re-storing is a harmless no-op - but
    # storing before the claim means a later put/ack failure can never lose the
    # learning (the claim, made only for the ACK, would otherwise short-circuit
    # the retry and drop the rule entirely). FLINT data-integrity fix.
    if classification is _LEARN_UNUSABLE or classification is _LEARN_DEFER_EXHAUSTED:
        # Nothing to store. Unlike the courtesy ack this notice is the
        # maintainer's only cue to re-reply, so it is at-least-once: no
        # win-once claim (a claim taken before a failed post would swallow
        # the retry), and a failed post raises for redrive. A rare SQS
        # duplicate delivery can repeat it; a lost one cannot be recovered.
        _react_to_learn_reply(
            install_id, owner, repo_name, comment_id, LEARN_REACTION_UNJUDGED,
        )
        result = (
            "learn_classifier_unusable" if classification is _LEARN_UNUSABLE
            else "learn_defer_exhausted"
        )
        log.info("learn_classified", extra={
            "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
            "result": result})
        return result
    ack, result, refused = _store_learning(
        install_id, repo_full, pr_number, comment_id, author, reply_text,
        record, classification, stored=already is not None,
    )

    # The claim guards ONLY the ack (the one non-idempotent side effect): a
    # redelivery whose learning is already stored must not re-post the reply.
    # A genuine RE-TEACH is a NEW reply comment (distinct claim), so it acks.
    if not claim_delivery(f"learn:{comment_id}"):
        log.info("learn_already_acked", extra={
            "repo": repo_full, "pr": pr_number, "comment_id": comment_id})
        return "learn_duplicate"

    # Best-effort: the learning is already durably stored, so a transient reply
    # failure must NOT redrive (which would re-classify + risk a wrong verdict)
    # nor raise - it just costs the courtesy ack, which a re-teach would repost.
    try:
        _react_to_learn_reply(install_id, owner, repo_name, comment_id, ack)
    except Exception as e:  # noqa: BLE001
        log.warning("learn_ack_post_failed", extra={
            "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
            "kind": type(e).__name__})
    # Collapsible acknowledgment section in the thread: durable = "Markings
    # remembered", one-off = explicit decline. Best-effort; never redrive on
    # ack failure.
    try:
        body = _learn_ack_body(
            repo_full, classification["learning"], classification["scope_path"],
            author, pr_number, classification["durable"], refused=refused,
        )
        _post_learn_reply(install_id, owner, repo_name, comment_id, body)
    except Exception as e:  # noqa: BLE001
        log.warning("learn_ack_comment_failed", extra={
            "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
            "kind": type(e).__name__})
    try:
        emit_gauge("grug.learnings.classified", 1)
    except Exception:  # noqa: BLE001
        pass
    # Living review (#1187): a maintainer who replied may have resolved the
    # thread too; the Resolved chip on Elder's comment is the in-place
    # acknowledgement (the reply itself only ever gets a reaction). Never
    # raises; the reaction-poll lane catches a resolve that lands later.
    _sync_living_review(install_id, owner, repo_name, pr_number)
    log.info("learn_classified", extra={
        "repo": repo_full, "pr": pr_number, "comment_id": comment_id,
        "result": result})
    return result


def _org_learning_approval_delay(install_id: int, repo_full: str) -> int:
    """The repo's org learning-approval delay in seconds (default 0 =
    active immediately). Best-effort: any store/GitHub read failure yields 0
    so a newly-taught learning activates immediately rather than stalling
    pending. The org config field is `learning_approval_delay_seconds`."""
    owner, _, repo_name = repo_full.partition("/")

    def _repo_id(token: str) -> int | None:
        r = httpx.get(
            f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo_name, safe='')}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
        r.raise_for_status()
        return int((r.json() or {}).get("id", 0))

    try:
        repo_id = with_install_token_retry(install_id, _repo_id)
    except Exception:  # noqa: BLE001 - best-effort; default to immediate
        log.warning("learning_approval_delay_lookup_failed", extra={
            "install_id": install_id, "repo": repo_full})
        return 0
    if not repo_id:
        return 0
    from adapters.install_store import get_repo_config  # type: ignore
    try:
        cfg = get_repo_config(install_id, repo_id)
    except Exception:  # noqa: BLE001 - store read failure; default immediate
        log.warning("learning_approval_delay_config_failed", extra={
            "install_id": install_id, "repo": repo_full, "repo_id": repo_id})
        return 0
    return int(cfg.get("learning_approval_delay_seconds") or 0)


def _post_learn_reply(
    install_id: int, owner: str, repo_name: str, comment_id: int, body: str,
) -> None:
    """Post a reply to the maintainer's review comment (same finding thread).
    GitHub dedupes identical replies, so a redelivery is harmless."""
    from urllib.parse import quote as _q

    def _post(token: str) -> None:
        _gh_post(
            token,
            f"{_GH_API}/repos/{_q(owner, safe='')}/{_q(repo_name, safe='')}"
            f"/pulls/comments/{int(comment_id)}/replies",
            {"body": body},
        )
    with_install_token_retry(install_id, _post)


def _learn_ack_body(
    repo_full: str, text: str, scope_path: str, author: str,
    source_pr: int, durable: bool, refused: bool = False,
) -> str:
    """Collapsible acknowledgment for a reply-mined learning. `text`/`scope`/
    `author` are defused (classifier-/reply-produced, untrusted) so a hostile
    reply can't break out of the <details> or inject mentions. `refused`
    means a durable rule that touches a security or SAST class (#1571)."""
    text_d = _defuse_md(text)
    scope_d = _defuse_md(scope_path)
    author_d = _defuse_md(author)
    scope_line = f", scoped to `{scope_d}`" if scope_d else ""
    if refused:
        return (
            "<details>\n"
            "<summary>Not remembered (security rule)</summary>\n\n"
            f"Grug read your reply but will NOT carry it into future reviews on "
            f"`{repo_full}`: learned rules never touch security or SAST findings. "
            "It applies to this PR only.\n\n"
            f"Replied by `{author_d}` (PR #{source_pr}).\n"
            "</details>"
        )
    if durable:
        return (
            "<details>\n"
            "<summary>Markings remembered</summary>\n\n"
            f"Grug remembered this preference for future reviews on `{repo_full}`:\n"
            f"- `{text_d}`{scope_line}\n\n"
            f"Taught by `{author_d}` (PR #{source_pr}). Grug will apply it to the "
            "next Elder review of this repo.\n"
            "</details>"
        )
    return (
        "<details>\n"
        "<summary>Not remembered (one reply only)</summary>\n\n"
        f"Grug read your reply but will NOT carry this into future reviews on "
        f"`{repo_full}`. It applies to this PR only.\n\n"
        f"Replied by `{author_d}` (PR #{source_pr}).\n"
        "</details>"
    )


def _hold_or_requeue_learn(
    job: dict[str, Any], remaining: float, receive_count: int, message_id: str,
) -> str:
    """A deferred learn job arrived before its `not_before`.

    On its first receive, ask the consumer to hide it until due (JobNotDue).
    A not-due job seen AGAIN means that hide failed, and every early receive
    counts toward the DLQ's maxReceiveCount - left alone, a visibility-API
    outage would dead-letter the reply. So a repeat early arrival is replaced
    by a fresh copy (its own receive count) and this one completes. If that
    send fails too, the raise redrives as usual."""
    if receive_count < 2 or not message_id:
        raise JobNotDue(remaining)
    enqueue_learn(
        install_id=int(job["install_id"]), repo=str(job["repo"]),
        pr_number=int(job["pr_number"]),
        comment_id=int(job.get("comment_id", 0)),
        parent_comment_id=int(job.get("parent_comment_id", 0)),
        reply_text=str(job.get("reply_text", "")),
        author=str(job.get("author", "")),
        defer_count=max(1, int(job.get("defer_count", 0) or 0)),
        not_before=float(job.get("not_before", 0) or 0),
        first_deferred_at=float(job.get("first_deferred_at", 0) or 0),
        requeue_of=message_id,
    )
    log.warning("learn_deferred_requeued", extra={
        "repo": str(job["repo"]), "pr": int(job["pr_number"]),
        "comment_id": int(job.get("comment_id", 0)),
        "receive_count": receive_count, "remaining_s": int(remaining)})
    return "learn_deferred"


def run_learn_job(
    job: dict[str, Any], *, receive_count: int = 0, message_id: str = "",
) -> str:
    """Run one decoded learn job from the rerun queue."""
    # A deferred job (every classifier backend was rate limited) arrives
    # at once; FIFO queues have no per-message delay. Hold it until due.
    remaining = float(job.get("not_before", 0) or 0) - _now()
    if remaining > 0:
        return _hold_or_requeue_learn(job, remaining, receive_count, message_id)
    return _run_learn(
        int(job["install_id"]), str(job["repo"]), int(job["pr_number"]),
        int(job.get("comment_id", 0)),
        int(job.get("parent_comment_id", 0)),
        str(job.get("reply_text", "")),
        str(job.get("author", "")),
        defer_count=int(job.get("defer_count", 0) or 0),
        first_deferred_at=float(job.get("first_deferred_at", 0) or 0),
    )
