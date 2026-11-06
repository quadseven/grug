# WEBHOOK-ONLY (NOT mirrored): the decisions of the durable Elder review job.
"""What happens next to one durable Elder review job (#1592).

`rerun._run_hot_review` runs a review job in three steps: it reads the live
PR, takes the snapshot claim, and dispatches Elder. After each step it asks
`decide` what comes next, and then performs only the effects: closing a
check, settling the claim, re-enqueuing, or raising for SQS redrive.

`decide` is pure. It reads the job, the live PR, the claim state, the
delivery count and the review result, and returns a `Decision`. Each rule
from an incident is one row in `tests/test_review_lifecycle.py`.

ADR-0023 (fail closed): a degraded review whose fail-open check did not
land never completes the job. `FAIL_OPEN` names the check to post; the
caller raises for redrive when the post fails.
"""
from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from personas.code_reviewer.dispatch import (
    DIFF_TOO_LARGE,
    RETRIED_DEGRADATIONS,
)
from personas.code_reviewer.removed_files import REMOVED_REFERENCE_CHECK_FAILED
from personas.review_publish import LOW_CONFIDENCE

# grug#1494: a review where every model failed (`all_failed`) raises, so the
# queue redelivers it. A partial or low-confidence review with nothing
# blocking in it rides the same bound: dispatch leaves it pending and marks
# the result `rerun_scheduled`, which raises below. Deliveries below this
# number leave a blocking repo's Elder check pending; from this one on the
# failure is published, so a long provider outage still ends in a red check
# instead of waiting forever. The queue's own dead-letter limit stays the
# outer bound.
ALL_FAILED_AUTO_RERUN_DELIVERIES = 3

# Skip reasons where a retry can plausibly succeed (model backend outage,
# unparseable model output, transient diff-fetch error). These raise for SQS
# redrive instead of terminal fail-open completion. Moved-head staleness,
# freshness brownouts, and unknown skips complete neutral; same-head
# staleness and ineligible (draft/closed) exits intentionally stay
# non-terminal - the requeued/reopen review completes the pending check.
# Sourced from the persona rather than restated, so the retry lane and the
# board/email gate cannot drift apart. `worth_an_email` suppresses the board
# for exactly these reasons on the grounds that a retry is coming; if this set
# grew a member here only, that promise would silently become a lie.
RETRYABLE_SKIP_REASONS = RETRIED_DEGRADATIONS

# Skips that ALREADY published their own terminal check, so the fail-open net
# must leave them alone. Re-posting a generic "Elder skipped - <reason>" over
# one of these replaces real content with strictly less.
#
# `partial_review` belongs here and was missing: it publishes a full check
# naming which cohorts completed, yet fell through to the "unknown skip"
# branch, which overwrote it. That is why one PR showed "Elder skipped -
# partial_review" on the check while the board said "Degraded (partial_review)
# - advisory only" - two surfaces describing one pass in different words,
# because two different code paths wrote them.
#
# DIFF_TOO_LARGE belongs here for the same reason: its degraded check already
# says the diff is over GitHub's size limit and what to do about it, and no
# retry changes GitHub's answer.
#
# LOW_CONFIDENCE (grug#1097) publishes its own advisory check the same way a
# partial review does.
#
# REMOVED_REFERENCE_CHECK_FAILED (grug#1401) publishes its own degraded check
# for a deletion-only diff, naming the guard findings it did make.
SELF_COMPLETING_SKIP_REASONS = frozenset({
    "no_diff",
    "fail_open_freshness",
    "partial_review",
    LOW_CONFIDENCE,
    DIFF_TOO_LARGE,
    REMOVED_REFERENCE_CHECK_FAILED,
})

# Results that are a finished review, so the job completes.
_REVIEWED_RESULTS = frozenset({"pass", "fail", "skipped", "review_rejected"})


class NextStep(enum.Enum):
    """The next step for a review job. The value is the logged step name."""

    REVIEW = "review"              # go on to the next stage of the job
    SUPERSEDE = "supersede"        # a newer snapshot owns the review
    REDRIVE = "redrive"            # raise, so SQS delivers the job again
    FAIL_OPEN = "fail_open"        # post a terminal degraded check, then complete
    HOLD_PENDING = "hold_pending"  # leave the check as it is; a later event settles it
    COMPLETE = "complete"          # the job is done
    GIVE_UP = "give_up"            # post a terminal failure check; no retry can help


class ClaimState(enum.Enum):
    """The snapshot claim, as `ReviewClaimLease.acquire` left it."""

    COMPLETED = "completed"  # an earlier job already reviewed this snapshot
    BUSY = "busy"            # another job holds the claim right now
    HELD = "held"            # this job holds the claim


@dataclass(frozen=True)
class Decision:
    """One decision. `status` is the job's short result for the batch log,
    or the redrive message when `step` is REDRIVE. `close_head_sha` names a
    head whose open Elder check closes neutral, when one does."""

    step: NextStep
    status: str
    close_head_sha: str = ""


@dataclass(frozen=True)
class PrSnapshot:
    """What `decide` reads from one live fetch of the PR."""

    head_sha: str
    state: str
    draft: bool
    snapshot_id: str

    @property
    def eligible(self) -> bool:
        return self.state == "open" and not self.draft

    @classmethod
    def from_pr(cls, pr: Mapping[str, Any], snapshot_id: str = "") -> PrSnapshot:
        return cls(
            head_sha=str((pr.get("head") or {}).get("sha") or ""),
            state=str(pr.get("state") or ""),
            draft=bool(pr.get("draft", False)),
            snapshot_id=snapshot_id,
        )


@dataclass(frozen=True)
class ReviewResult:
    """What `decide` reads from a finished `dispatch_code_review`. `result`
    is kept as dispatch returned it, so an unexpected value is named as is."""

    result: Any
    degraded_reason: str = ""
    rerun_scheduled: bool = False

    @classmethod
    def from_dispatch(cls, result: Mapping[str, Any]) -> ReviewResult:
        return cls(
            result=result.get("result"),
            degraded_reason=str(result.get("degraded_reason", "") or ""),
            rerun_scheduled=bool(result.get("rerun_scheduled")),
        )


def rerun_pending(delivery_count: int) -> bool:
    """True while a failed review still has a queue re-run ahead of it, so
    dispatch leaves a blocking repo's check pending instead of failing it."""
    return 1 <= delivery_count < ALL_FAILED_AUTO_RERUN_DELIVERIES


def decide(
    job: Mapping[str, Any],
    pr_snapshot: PrSnapshot | None,
    claim_state: ClaimState | None = None,
    review_result: ReviewResult | None = None,
    *,
    claimed_head_sha: str = "",
    claimed_snapshot_id: str = "",
) -> Decision:
    """The next step for a review job, from what is known so far.

    - `claim_state` None: the job just read the PR (`pr_snapshot` None
      means GitHub refused the read with 403).
    - `review_result` None with a claim: the claim is taken, and with
      `ClaimState.HELD`, `pr_snapshot` is the fetch after the settle wait
      and `claimed_snapshot_id` the snapshot the claim is for.
    - `review_result` set: Elder ran on `claimed_head_sha`; `pr_snapshot`
      is the fetch after a `stale_snapshot` result and unused otherwise.

    The delivery count reaches the decision through dispatch: it reads
    `rerun_pending(delivery_count)` and reports back `rerun_scheduled`."""
    if review_result is not None:
        return _after_review(pr_snapshot, review_result, claimed_head_sha)
    if claim_state is None:
        return _at_entry(job, pr_snapshot)
    return _after_claim(pr_snapshot, claim_state, claimed_snapshot_id)


def _at_entry(job: Mapping[str, Any], pr: PrSnapshot | None) -> Decision:
    requested = str(job.get("requested_head_sha") or "")
    if pr is None:
        # A 403 is the installation's access, not a blip: a redrive gets the
        # same answer, so the check fails and the job ends.
        return Decision(NextStep.GIVE_UP, "pr_read_forbidden")
    # grug#892: a newer push already enqueued its own job for the new head.
    if requested and requested != pr.head_sha:
        return Decision(NextStep.SUPERSEDE, "superseded_at_entry", close_head_sha=requested)
    if pr.draft:
        return Decision(NextStep.HOLD_PENDING, "draft_skipped")
    if pr.state != "open":
        return Decision(NextStep.HOLD_PENDING, "pr_ineligible")
    return Decision(NextStep.REVIEW, "claim")


def _after_claim(
    pr: PrSnapshot | None, claim: ClaimState, claimed_snapshot_id: str,
) -> Decision:
    if claim is ClaimState.COMPLETED:
        return Decision(NextStep.COMPLETE, "duplicate_snapshot")
    if claim is ClaimState.BUSY:
        return Decision(NextStep.REDRIVE, "Elder review snapshot claim is still in progress")
    if pr is None:
        raise ValueError("a held claim needs the PR fetched after the settle wait")
    if not pr.eligible:
        return Decision(NextStep.HOLD_PENDING, "pr_ineligible")
    if pr.snapshot_id != claimed_snapshot_id:
        return Decision(NextStep.SUPERSEDE, "stale_snapshot")
    return Decision(NextStep.REVIEW, "dispatch")


def _after_review(
    latest: PrSnapshot | None, review: ReviewResult, claimed_head: str,
) -> Decision:
    reason = review.degraded_reason
    if reason == "stale_snapshot":
        if latest is None:
            raise ValueError("a stale_snapshot result needs the PR fetched again")
        # The head moved: close the abandoned head's check. Same head (a
        # title or body edit): leave it in_progress for the requeued review,
        # since a neutral here would green the merge button early.
        close = ""
        if latest.head_sha and latest.head_sha != claimed_head:
            close = claimed_head
        if not latest.eligible:
            return Decision(NextStep.HOLD_PENDING, "pr_ineligible", close_head_sha=close)
        return Decision(NextStep.SUPERSEDE, "stale_snapshot", close_head_sha=close)
    if reason == "pr_ineligible":
        # No terminal check: a neutral would satisfy the required check the
        # moment the PR is reopened, before the fresh review posts.
        return Decision(NextStep.HOLD_PENDING, "pr_ineligible")
    status = review.result
    if status == "publish_failed":
        return Decision(NextStep.REDRIVE, "Elder review publication failed")
    # `review_rejected` (#770) completes like pass/fail: GitHub's 4xx on the
    # inline review is deterministic, and the check already landed.
    if status == "skipped" and reason == "freshness_check_failed":
        return Decision(NextStep.FAIL_OPEN, "fail_open_freshness")
    if status == "skipped" and reason in RETRYABLE_SKIP_REASONS:
        return Decision(NextStep.REDRIVE, f"Elder review degraded: {reason}")
    if status == "skipped" and review.rerun_scheduled:
        # grug#1494: the queue's re-run is the bounded lane; the last
        # delivery publishes the degraded failure and completes.
        return Decision(
            NextStep.REDRIVE, f"Elder review degraded: {reason}; re-run scheduled",
        )
    if status == "skipped" and reason not in SELF_COMPLETING_SKIP_REASONS:
        # An unknown skip completes the check rather than redrive forever.
        return Decision(NextStep.FAIL_OPEN, f"fail_open_{reason or 'skipped'}")
    if status not in _REVIEWED_RESULTS and not str(status).startswith("fail_open"):
        return Decision(
            NextStep.REDRIVE, f"Elder review returned unexpected result: {status!r}",
        )
    return Decision(NextStep.COMPLETE, "dispatched")
