# MIRRORED — sibling at services/api/personas/code_reviewer/reactions.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Reaction-poll engine for the Elder persona (#245).

The second of the two #190 quality-feedback loops: a developer's 👍/👎
on a Grug inline review comment is the HUMAN ground-truth label that
calibrates the LLM judge's `is_real_bug` guess. GitHub does not webhook
comment reactions, so a scheduled poller (#245b) reads them via the
reactions REST API and pipes the signal into DD LLM Obs as a
`human_verdict` annotation attached to the review span.

This module is the engine: classify reactions → verdict, poll a
comment's reactions, and `poll_and_annotate` a batch with dedup. The
scheduled trigger (Lambda + EventBridge) + the persist-on-publish
wiring live in #245b — this layer is pure logic + GH/DD I/O, fully
unit-testable.

Dedup: each comment record carries `last_verdict` (the verdict last
submitted to DD). We only submit when the current classification
differs — so a 👎 that's been sitting for days doesn't re-submit every
poll cycle, but a developer flipping 👎→👍 (changed their mind) does.
Dedup is at-least-once, NOT exactly-once: if the DD submit succeeds but
the baseline write then fails, the next cycle re-submits. DD LLM Obs
evaluations are time-series events (each `submit_evaluation` carries a
generation timestamp), so a re-submit APPENDS a second `human_verdict`
event on the span — it does NOT overwrite. We accept that: a rare
duplicate (only on a submit-ok / baseline-write-fail partial failure,
which needs a DDB fault) is strictly better than advancing the baseline
before a confirmed submit and losing a human 👍/👎 forever. The
calibration consumer MUST dedup by (span, label) taking the latest
timestamp — do not assume one eval per comment.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

import httpx

from adapters.install_store import CommentRecord, update_comment_record_reaction
from llm_client import ReactionVerdict, submit_reaction_annotation

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.reactions")

_GH_API = "https://api.github.com"
_REACTIONS_TIMEOUT = 10


def _classify_reactions(reactions: list[dict]) -> Optional[ReactionVerdict]:
    """Map a comment's reactions to a single verdict, or None.

    👎 (`-1`) → false_positive; 👍 (`+1`) → confirmed. When BOTH are
    present, 👎 wins: a developer flagging a false positive is the
    higher-value correction (it tells us the reviewer was wrong, which
    is what prompt-optimization needs most). Non-thumbs reactions
    (heart/rocket/eyes) carry no verdict signal → None.

    This classifies WHATEVER reactions it's handed — it does NOT filter
    by who reacted. A passerby's 👎 currently counts the same as the PR
    author's. Filtering to author/collaborator reactions needs the PR
    author identity (captured at persist time) and is tracked in the
    dispatch-wiring slice (#247). Until then the asymmetric 👎-wins rule
    is bias-prone: the mixed-signal log (below) marks contested rows so
    the calibration set can DOWN-WEIGHT them, not merely filter.
    """
    contents = {r.get("content") for r in reactions}
    if "-1" in contents:
        return "false_positive"
    if "+1" in contents:
        return "confirmed"
    return None


def poll_comment_reactions(
    install_token: str, owner: str, repo: str, comment_id: int,
) -> list[dict]:
    """GET the reactions on one PR review comment. Raises on transport /
    HTTP error — the batch caller catches per-record so one failure
    doesn't abort the cycle.

    `per_page=100` (the max) — the endpoint defaults to 30/page and
    returns reactions in created_at order. Without this, a 👎 sitting
    past position 30 on a heavily-reacted comment would never be seen
    and the comment would misclassify as confirmed/None — defeating the
    "👎 wins" calibration premise. 100 makes a miss effectively
    impossible for a single review comment (full Link-header pagination
    would be over-engineering at that ceiling)."""
    resp = httpx.get(
        f"{_GH_API}/repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions",
        params={"per_page": 100},
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_REACTIONS_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, list) else []


def poll_and_annotate(
    records: list[CommentRecord],
    *,
    install_id: int,
    fetch_token: Callable[[], str],
) -> int:
    """Poll reactions for each comment record and submit changed
    verdicts to DD. Returns the count submitted.

    Best-effort per-record: a single comment's poll failing (GH 5xx,
    deleted comment) logs + continues — one bad comment must not abort
    the whole poll cycle. `fetch_token` is a thunk so the caller owns
    install-token acquisition (and we don't re-fetch per record beyond
    what the thunk caches).
    """
    submitted = 0
    for rec in records:
        comment_id = rec["comment_id"]
        span_context = rec.get("review_span_context")
        if span_context is None:
            # `CommentRecord` types this `Optional[dict]`: None means the
            # review span never exported (degraded review at publish), so
            # there's nothing to attach the annotation to. Skip.
            continue
        # `partition` (not `split` + splat): a malformed persisted repo
        # without a "/" would make `*split("/",1)` a 1-element splat →
        # uncaught TypeError → aborts the whole batch, breaking the
        # per-record best-effort contract. Guard + skip instead.
        owner, sep, name = rec["repo"].partition("/")
        if not sep:
            log.warning(
                "reaction_record_malformed_repo",
                extra={"install_id": install_id, "comment_id": comment_id,
                       "repo": rec["repo"]},
            )
            continue
        try:
            reactions = poll_comment_reactions(
                fetch_token(), owner, name, comment_id,
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.warning(
                "reaction_poll_failed",
                extra={
                    "install_id": install_id, "comment_id": comment_id,
                    "kind": type(e).__name__,
                },
            )
            continue
        verdict = _classify_reactions(reactions)
        if verdict is None:
            continue
        if verdict == rec.get("last_verdict"):
            # Dedup: already submitted this verdict for this comment.
            continue
        # Mixed-signal observability: a comment with BOTH 👍 and 👎 is a
        # developer disagreement — the highest-information calibration
        # case, force-classified to false_positive. Log it (with the
        # verdict) so the calibration set can DOWN-WEIGHT contested rows
        # rather than have them silently flatten into the verdict.
        contents = {r.get("content") for r in reactions}
        if "+1" in contents and "-1" in contents:
            log.info(
                "reaction_mixed_signal",
                extra={"install_id": install_id, "comment_id": comment_id,
                       "verdict": verdict},
            )
        # Guard the DD-submit + baseline-write as a unit. Broad catch:
        # both the ddtrace seam (internal/intake errors, a stale
        # malformed span dict) and the store update (psycopg.Error:
        # throttle, timeout) can raise, and a best-effort poller must
        # not let one bad record abort the rest of the batch. Submit
        # FIRST, advance the baseline only after — so on a partial
        # failure we re-submit next cycle rather than lose the human
        # signal forever. A re-submit APPENDS a duplicate eval (DD evals
        # are time-series, not upserts) — accepted; the calibration
        # consumer dedups by (span, label). See module docstring.
        try:
            submit_reaction_annotation(
                verdict=verdict,
                review_span_context=span_context,
                tags=rec.get("finding_tags", {}),
            )
            update_comment_record_reaction(
                install_id=install_id, comment_id=comment_id, verdict=verdict,
            )
            submitted += 1
        except Exception as e:  # noqa: BLE001 — best-effort per-record
            log.warning(
                "reaction_submit_or_persist_failed",
                extra={"install_id": install_id, "comment_id": comment_id,
                       "kind": type(e).__name__},
            )
            continue
    log.info(
        "reaction_poll_cycle",
        extra={
            "install_id": install_id,
            "records": len(records),
            "submitted": submitted,
        },
    )
    return submitted
