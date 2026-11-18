"""Characterization table for the durable Elder review job (#1592).

One row per rule. Each row names the incident or ADR that set it. The rows
pin what `rerun._run_hot_review` did before its decisions moved into
`review_lifecycle.decide`; `test_rerun.py` still drives the effects.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import review_lifecycle as rl
from review_lifecycle import ClaimState, NextStep, PrSnapshot, ReviewResult

S, RD, FO, HP, C, GU, RV = (
    NextStep.SUPERSEDE, NextStep.REDRIVE, NextStep.FAIL_OPEN,
    NextStep.HOLD_PENDING, NextStep.COMPLETE, NextStep.GIVE_UP, NextStep.REVIEW,
)


def _pr(head="h1", state="open", draft=False, snap="s1") -> PrSnapshot:
    return PrSnapshot(head_sha=head, state=state, draft=draft, snapshot_id=snap)


def _res(result="pass", reason="", rerun=False) -> ReviewResult:
    return ReviewResult(result=result, degraded_reason=reason, rerun_scheduled=rerun)


# (source, requested_head_sha, pr, step, status, close_head_sha)
ENTRY = [
    ("403 read is config, not a blip", "h1", None, GU, "pr_read_forbidden", ""),
    ("#892 newer push owns the review", "h0", _pr(), S, "superseded_at_entry", "h0"),
    ("#892 no requested head: no supersede", "", _pr(), RV, "claim", ""),
    ("draft: wait for ready_for_review", "h1", _pr(draft=True), HP, "draft_skipped", ""),
    ("closed: nothing to review", "h1", _pr(state="closed"), HP, "pr_ineligible", ""),
    ("current head, open: take the claim", "h1", _pr(), RV, "claim", ""),
]

# (source, claim, pr after settle, claimed snapshot, step, status)
CLAIM = [
    ("snapshot already reviewed", ClaimState.COMPLETED, None, "s1", C, "duplicate_snapshot"),
    ("claim busy: redrive, never run twice", ClaimState.BUSY, None, "s1", RD,
     "Elder review snapshot claim is still in progress"),
    ("went draft during settle", ClaimState.HELD, _pr(draft=True), "s1", HP, "pr_ineligible"),
    ("closed during settle", ClaimState.HELD, _pr(state="closed"), "s1", HP, "pr_ineligible"),
    ("snapshot moved during settle", ClaimState.HELD, _pr(snap="s2"), "s1", S, "stale_snapshot"),
    ("snapshot unchanged: dispatch", ClaimState.HELD, _pr(), "s1", RV, "dispatch"),
]

# (source, review result, pr after a stale result, step, status, close_head_sha)
REVIEW = [
    ("head moved mid-review: close old head", _res("skipped", "stale_snapshot"),
     _pr(head="h2"), S, "stale_snapshot", "h1"),
    ("same head (intent edit): leave check pending", _res("skipped", "stale_snapshot"),
     _pr(head="h1"), S, "stale_snapshot", ""),
    ("stale and now closed: hold, close old head", _res("skipped", "stale_snapshot"),
     _pr(head="h2", state="closed"), HP, "pr_ineligible", "h1"),
    ("draft mid-review: no terminal neutral", _res("skipped", "pr_ineligible"), None,
     HP, "pr_ineligible", ""),
    ("publish failed: redrive", _res("publish_failed"), None,
     RD, "Elder review publication failed", ""),
    ("#770 review_rejected completes", _res("review_rejected"), None, C, "dispatched", ""),
    ("ADR-0023 freshness outage fails open on the last delivery",
     _res("skipped", "freshness_check_failed"), None, FO, "fail_open_freshness", ""),
    ("#818 freshness outage with a re-run ahead redrives",
     _res("skipped", "freshness_check_failed", True), None,
     RD, "Elder review degraded: freshness_check_failed; re-run scheduled", ""),
    ("#1494 all_failed redrives", _res("skipped", "all_failed"), None,
     RD, "Elder review degraded: all_failed", ""),
    ("#1534 partial with a re-run scheduled redrives", _res("skipped", "partial_review", True),
     None, RD, "Elder review degraded: partial_review; re-run scheduled", ""),
    ("partial_review publishes its own check", _res("skipped", "partial_review"), None,
     C, "dispatched", ""),
    ("no_diff completes", _res("skipped", "no_diff"), None, C, "dispatched", ""),
    ("unknown skip fails open", _res("skipped", "mystery"), None,
     FO, "fail_open_mystery", ""),
    ("unknown skip without a reason", _res("skipped", ""), None, FO, "fail_open_skipped", ""),
    ("pass completes", _res("pass"), None, C, "dispatched", ""),
    ("fail completes", _res("fail"), None, C, "dispatched", ""),
    ("dispatch fail-open result completes", _res("fail_open_x"), None, C, "dispatched", ""),
    ("unexpected result redrives", _res("weird"), None,
     RD, "Elder review returned unexpected result: 'weird'", ""),
    ("no result at all redrives", _res(None), None,
     RD, "Elder review returned unexpected result: None", ""),
]


@pytest.mark.parametrize(("source", "requested", "pr", "step", "status", "close"), ENTRY,
                         ids=[r[0] for r in ENTRY])
def test_entry(source, requested, pr, step, status, close):
    d = rl.decide({"requested_head_sha": requested}, pr)
    assert (d.step, d.status, d.close_head_sha) == (step, status, close)


@pytest.mark.parametrize(("source", "claim", "pr", "claimed", "step", "status"), CLAIM,
                         ids=[r[0] for r in CLAIM])
def test_after_claim(source, claim, pr, claimed, step, status):
    d = rl.decide({}, pr, claim, claimed_snapshot_id=claimed)
    assert (d.step, d.status) == (step, status)


@pytest.mark.parametrize(("source", "review", "pr", "step", "status", "close"), REVIEW,
                         ids=[r[0] for r in REVIEW])
def test_after_review(source, review, pr, step, status, close):
    d = rl.decide({}, pr, ClaimState.HELD, review, claimed_head_sha="h1")
    assert (d.step, d.status, d.close_head_sha) == (step, status, close)


def test_every_retried_degradation_redrives():
    for reason in sorted(rl.RETRYABLE_SKIP_REASONS):
        assert rl.decide({}, None, ClaimState.HELD, _res("skipped", reason)).step is RD


def test_every_self_completing_skip_completes():
    for reason in sorted(rl.SELF_COMPLETING_SKIP_REASONS):
        assert rl.decide({}, None, ClaimState.HELD, _res("skipped", reason)).step is C


@pytest.mark.parametrize(("delivery", "pending"), [
    (0, False), (1, True), (2, True), (3, True), (4, False), (5, False),
])
def test_rerun_pending_bounds_the_deferred_failure(delivery, pending):
    """#1494, #818: deliveries 1 to 3 leave the check pending; from the fourth
    (the queue's last-but-one) the failure is published."""
    assert rl.rerun_pending(delivery) is pending


def test_the_rerun_bound_is_the_review_queues_own_dead_letter_limit():
    """#818: retries run out when the queue's do, not two deliveries early.
    The terminal publish takes the last-but-one delivery, so one is left for
    a consumer killed mid-publish."""
    main = Path(__file__).resolve().parents[3] / "infra" / "pulumi" / "__main__.py"
    limit = re.search(
        r"_rerun_dlq\.arn\.apply\(\s*lambda arn: json\.dumps\("
        r'\{"deadLetterTargetArn": arn, "maxReceiveCount": (\d+)\}',
        main.read_text(),
    )
    assert limit, "the review queue's redrive policy moved; re-point this test"
    assert int(limit.group(1)) == rl.REVIEW_QUEUE_MAX_RECEIVES
    assert rl.ALL_FAILED_AUTO_RERUN_DELIVERIES == rl.REVIEW_QUEUE_MAX_RECEIVES - 1


def test_step_names_are_the_logged_values():
    assert [s.value for s in NextStep] == [
        "review", "supersede", "redrive", "fail_open", "hold_pending",
        "complete", "give_up",
    ]


def test_snapshot_reads_a_github_pr():
    snap = PrSnapshot.from_pr({"head": {"sha": "abc"}, "state": "open", "draft": True}, "s9")
    assert snap == PrSnapshot("abc", "open", True, "s9") and not snap.eligible


def test_review_result_reads_a_dispatch_result():
    got = ReviewResult.from_dispatch({"result": "skipped", "degraded_reason": None,
                                      "rerun_scheduled": 1})
    assert got == ReviewResult("skipped", "", True)


@pytest.mark.parametrize(("reason", "ahead", "scheduled"), [
    ("freshness_check_failed", True, True),
    ("freshness_check_failed", False, False),
    ("mystery", True, False),
])
def test_a_transient_fail_open_counts_as_rerun_scheduled_while_one_is_ahead(
    reason, ahead, scheduled,
):
    """#818: a GitHub brownout is retried like a model outage before the
    check fails open; an unknown skip is not."""
    got = ReviewResult.from_dispatch(
        {"result": "skipped", "degraded_reason": reason}, rerun_ahead=ahead,
    )
    assert got.rerun_scheduled is scheduled
