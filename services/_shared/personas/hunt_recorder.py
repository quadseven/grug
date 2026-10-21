"""Feeds persona runs and human decisions into the canonical review session,
then refreshes the Hunt Board projection.

`review_session` is the pure model and `hunt_board` the GitHub projection;
this is the seam between them and the rest of grug: it owns the store
round trip (`update_session`, compare-and-swap on the HUNT# row), the
timestamps, and the publish that follows a write.

BEST-EFFORT BY DESIGN. The session is a record of a review that already
happened, so a store or GitHub failure here must never fail that review. Every
entry point returns None on failure after logging `hunt_session_update_failed`
with the error kind (an unexpected kind is logged at error with its traceback); the next run or event re-applies its mutation and heals
the record.

RETRIES. A persona run is keyed (persona, head sha, trigger). A webhook that
is delivered twice replays the key of a run that already completed, and the
session is returned unchanged, so nothing is written and nothing is
republished. A failed run does not block its redelivery (that is the retry
that should succeed), and an explicit `rerun` is a new decision by a person,
so it always records a new run.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Optional

import httpx
import psycopg

import review_session as rs
from github_app_auth import with_install_token_retry
from personas import hunt_board

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.personas.hunt_recorder")

# The trigger of a run a person asked for (also the prefix of its deep
# append, `rerun:deep`); never treated as a retry.
RERUN_TRIGGER = "rerun"

# Failures a store or GitHub outage produces; any other kind is unexpected.
_EXPECTED_FAILURES = (psycopg.Error, httpx.HTTPError, rs.SessionConflict)

Anchors = Callable[[tuple[rs.FindingRecord, ...]], Mapping[str, rs.AnchorStatus]]


@dataclass(frozen=True)
class PrRef:
    install_id: int
    owner: str
    repo: str
    pr_number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class RunMeta:
    """What a persona run says about itself, for the session's run log."""

    persona: str
    head_sha: str
    trigger: str
    started_at: str
    backend: str = ""
    model: str = ""

    @property
    def run_id(self) -> str:
        return f"{self.persona}:{self.head_sha}:{self.trigger}:{self.started_at}"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_retry(session: rs.ReviewSession, meta: RunMeta) -> bool:
    """True when this delivery repeats a run that already completed."""
    if meta.trigger.startswith(RERUN_TRIGGER):
        return False
    return any(
        (r.persona, r.head_sha, r.trigger, r.status)
        == (meta.persona, meta.head_sha, meta.trigger, "complete")
        for r in session.runs
    )


def _begin(session: rs.ReviewSession, meta: RunMeta) -> rs.ReviewSession:
    return rs.start_run(session, rs.ReviewRun(
        run_id=meta.run_id, persona=meta.persona, head_sha=meta.head_sha,
        trigger=meta.trigger, started_at=meta.started_at,
        backend=meta.backend, model=meta.model,
    ))


def _log_failure(event: str, ref: PrRef, e: Exception) -> None:
    """Log a swallowed failure. The expected kinds (store, transport, lost
    race) are a warning; anything else is a bug in this seam and is logged
    at error with its traceback, so best-effort never hides it."""
    extra = {"pr": f"{ref.full_name}#{ref.pr_number}", "kind": type(e).__name__}
    if isinstance(e, _EXPECTED_FAILURES):
        log.warning(event, extra=extra)
    else:
        log.error(event, extra=extra, exc_info=e)


def _update(
    ref: PrRef, mutate: Callable[[rs.ReviewSession], rs.ReviewSession],
) -> Optional[rs.ReviewSession]:
    from adapters.install_store import get_review_session, put_review_session

    kw = {"install_id": ref.install_id, "repo": ref.full_name,
          "pr_number": ref.pr_number}
    try:
        session = rs.update_session(
            lambda: get_review_session(**kw),
            lambda data, expected: put_review_session(
                **kw, data=data, expected_version=expected),
            ref.full_name, ref.pr_number, mutate, at=now(),
        )
    except Exception as e:  # noqa: BLE001 - the record must not fail the review
        _log_failure("hunt_session_update_failed", ref, e)
        return None
    _publish(ref, session)
    return session


def _publish(ref: PrRef, session: rs.ReviewSession) -> None:
    # `hunt_board.publish` skips an unchanged or older projection itself and
    # logs its own failures; only a token failure can reach here.
    try:
        with_install_token_retry(
            ref.install_id,
            lambda token: hunt_board.publish(
                token, ref.owner, ref.repo, ref.pr_number, session),
        )
    except Exception as e:  # noqa: BLE001 - the projection is cosmetic
        _log_failure("hunt_board_publish_failed", ref, e)


def record_run(
    ref: PrRef,
    meta: RunMeta,
    *,
    findings: Iterable[rs.ObservedFinding],
    anchors: Anchors,
    check_run_id: Optional[int] = None,
    error: str = "",
) -> Optional[rs.ReviewSession]:
    """Record one finished persona run and reconcile its findings.

    `error` marks a run that could not finish: its persona's findings are
    left exactly as they were. `anchors` is asked about the prior findings
    only once the session is loaded, because only then are they known.
    """
    observed = tuple(findings)
    done_at = now()

    def mutate(session: rs.ReviewSession) -> rs.ReviewSession:
        if _is_retry(session, meta):
            return session
        began = _begin(session, meta)
        if error:
            return rs.fail_run(began, meta.run_id, at=done_at, error=error)
        return rs.complete_run(
            began, meta.run_id, findings=observed,
            anchors=anchors(session.findings), at=done_at,
            check_run_id=check_run_id,
        )

    return _update(ref, mutate)


def record_walkthrough(
    ref: PrRef,
    *,
    head_sha: str,
    trigger: str,
    started_at: str,
    backend: str,
    model: str,
    text: str,
    related: tuple[str, ...],
) -> Optional[rs.ReviewSession]:
    """Record Teller's run and keep its summary and related context."""
    meta = RunMeta("teller", head_sha, trigger, started_at, backend, model)
    done_at = now()

    def mutate(session: rs.ReviewSession) -> rs.ReviewSession:
        if _is_retry(session, meta):
            return session
        done = rs.complete_run(
            _begin(session, meta), meta.run_id, findings=(), anchors={}, at=done_at,
        )
        return rs.set_walkthrough(done, text, related=related)

    return _update(ref, mutate)


def record_comment_event(
    ref: PrRef, comment_id: int, event: str, *, sha: str, reason: str = "",
) -> Optional[rs.ReviewSession]:
    """A person acted on an inline comment (thumbs-down, resolve, reopen):
    apply it to the finding that comment belongs to. A comment that links to
    no finding (an older review, or another bot's) changes nothing."""
    at = now()

    def mutate(session: rs.ReviewSession) -> rs.ReviewSession:
        rec = rs.finding_for_comment(session, comment_id)
        if rec is None:
            return session
        return rs.apply_event(session, rec.finding_id, event, sha=sha, at=at, reason=reason)

    return _update(ref, mutate)
