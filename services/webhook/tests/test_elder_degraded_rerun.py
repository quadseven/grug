"""A partial or low-confidence Elder review gets the bounded automatic re-run.

#1494 gave `all_failed` a bounded automatic re-run: while a re-run is
still scheduled, a blocking repo's Elder check stays pending instead of
completing `failure`. A partial review (a cohort failed or was never walked)
and a low-confidence review are degraded the same way and fail closed the
same way (#1212), but they were never re-run, so their red check sat
until someone commented `/grug improve`.

These tests pin the widened rule:
1. A partial review with a re-run still scheduled reads pending, and the
   durable lane raises so the queue re-runs it.
2. A low-confidence review does the same.
3. The bound is the one all_failed uses: the last scheduled delivery
   publishes the failure and completes.
4. A degraded review that still found a blocking problem is a real verdict:
   it publishes `failure` at once and is not re-run.
5. WARN mode stays neutral, as before.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from llm_client import Backend, LlmReviewResponse
from llm_client import Finding as LlmFinding
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.persona import CodeReviewEvaluation
from review_pipeline import ReviewCoverage

from tests.test_elder_degraded_fail_closed import (
    _diff_response,
    _job,
    _patch_claims,
    _payload,
    _pr_data,
)

_PARTIAL = LlmReviewResponse(
    kind="reviewed",
    error="partial review: cohorts [2] failed",
    backend_used=Backend.POOLSIDE,
    coverage=ReviewCoverage(
        total_cohorts=2, completed_cohorts=1, failed_cohorts=(2,),
        cohort_labels=("a", "b"),
    ),
)
_LOW_CONFIDENCE = LlmReviewResponse(
    kind="reviewed",
    error="low confidence: short empty answer, no other model answered",
    backend_used=Backend.POOLSIDE,
)
_PARTIAL_WITH_BLOCKER = LlmReviewResponse(
    kind="reviewed",
    error="partial review: cohorts [2] failed",
    findings=(LlmFinding(
        path="src/x.py", line=2, rule="silent-failure",
        severity="high", message="error is discarded",
    ),),
    backend_used=Backend.POOLSIDE,
    coverage=ReviewCoverage(
        total_cohorts=2, completed_cohorts=1, failed_cohorts=(2,),
        cohort_labels=("a", "b"),
    ),
)
_DEGRADED = {
    "partial_review": _PARTIAL,
    "low_confidence_review": _LOW_CONFIDENCE,
}


@pytest.fixture
def elder(monkeypatch):
    monkeypatch.setattr(cr_dispatch, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setenv("GRUG_DEEP_ASYNC", "0")
    posted: list = []
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted.append(result) or {"id": 1}
        ),
    )
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 2})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())

    def run(llm: LlmReviewResponse, *, blocking: bool, rerun_pending: bool):
        monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
        # Every later model call (judge, refute) fails fast, offline.
        with (
            patch("httpx.get", return_value=_diff_response()),
            patch.object(httpx, "post", side_effect=httpx.ConnectError("offline")),
        ):
            result = cr_dispatch.dispatch_code_review(
                _payload(), blocking=blocking, rerun_pending=rerun_pending,
            )
        return result, posted

    return run


@pytest.mark.parametrize("reason", sorted(_DEGRADED))
def test_degraded_review_with_a_rerun_scheduled_reads_pending_when_blocking(
    elder, reason,
) -> None:
    result, posted = elder(_DEGRADED[reason], blocking=True, rerun_pending=True)

    assert result["degraded_reason"] == reason
    assert result["result"] == "skipped"
    assert result.get("rerun_scheduled") == "true"
    assert [(c.status, c.conclusion) for c in posted] == [("in_progress", None)]
    assert "re-run" in posted[0].summary.lower()
    assert reason in posted[0].summary


@pytest.mark.parametrize("reason", sorted(_DEGRADED))
def test_degraded_review_with_no_rerun_left_still_fails_when_blocking(
    elder, reason,
) -> None:
    result, posted = elder(_DEGRADED[reason], blocking=True, rerun_pending=False)

    assert "rerun_scheduled" not in result
    assert [(c.status, c.conclusion) for c in posted] == [("completed", "failure")]


@pytest.mark.parametrize("reason", sorted(_DEGRADED))
def test_degraded_review_in_warn_mode_stays_neutral(elder, reason) -> None:
    result, posted = elder(_DEGRADED[reason], blocking=False, rerun_pending=True)

    assert "rerun_scheduled" not in result
    assert [(c.status, c.conclusion) for c in posted] == [("completed", "neutral")]


def test_a_partial_review_with_a_blocking_finding_is_not_rerun(elder) -> None:
    """The blocking finding is a real verdict: it fails at once, the way a
    full review with that finding would, and nothing schedules a re-run."""
    result, posted = elder(_PARTIAL_WITH_BLOCKER, blocking=True, rerun_pending=True)

    assert "rerun_scheduled" not in result
    assert [(c.status, c.conclusion) for c in posted] == [("completed", "failure")]


@pytest.mark.parametrize(
    "reason",
    ["parse_failed", "fetch_or_parse_failed", "diff_too_large", "no_diff", "unknown_reason"],
)
def test_a_reason_outside_the_rerun_set_is_not_held_pending(reason) -> None:
    """Only all_failed and the two publishable degradations wait for a
    re-run. Any other degraded reason publishes as before."""
    evaluation = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason=reason,
    )

    assert cr_dispatch._rerun_before_failing(evaluation) is False


def test_parse_failed_is_not_held_pending_by_dispatch(elder) -> None:
    """End to end: a parse_failed pass with a re-run scheduled still
    publishes its completed failure and asks for no re-run here."""
    result, posted = elder(
        LlmReviewResponse(kind="parse_failed", error="prose, not JSON"),
        blocking=True, rerun_pending=True,
    )

    assert "rerun_scheduled" not in result
    assert [(c.status, c.conclusion) for c in posted] == [("completed", "failure")]


def test_all_failed_keeps_its_existing_pending_shape(elder) -> None:
    result, posted = elder(
        LlmReviewResponse(kind="all_failed", error="poolside: http_429"),
        blocking=True, rerun_pending=True,
    )

    assert result["degraded_reason"] == "all_failed"
    assert [(c.status, c.conclusion) for c in posted] == [("in_progress", None)]
    assert "every model failed" in posted[0].summary.lower()


# --- the durable lane --------------------------------------------------------

def _durable(monkeypatch, *, reason: str, receive_count: int):
    """Drive one durable review job. The dispatch stand-in answers the way
    the real one does: pending plus `rerun_scheduled` while a re-run is
    still scheduled, the published degraded result on the last one."""
    import rerun

    pr = _pr_data()
    fetches = iter((pr, pr))

    def token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")

    def fake_dispatch(payload, *, blocking, cancel_event, rerun_pending):
        out = {"persona": "code_reviewer", "result": "skipped", "degraded_reason": reason}
        if rerun_pending and blocking:
            out["rerun_scheduled"] = "true"
        return out

    dispatch = MagicMock(side_effect=fake_dispatch)
    posted: list = []
    monkeypatch.setattr(rerun, "with_install_token_retry", token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run", lambda *a, **kw: posted.append(a) or {"id": 1},
    )
    monkeypatch.setattr(
        rerun, "get_repo_config", lambda iid, rid: {"code_reviewer_blocking": True},
    )
    monkeypatch.setattr(rerun, "_set_board_in_progress_note", lambda *a: None)
    _patch_claims(monkeypatch)
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)
    return rerun, dispatch


@pytest.mark.parametrize("reason", sorted(_DEGRADED))
@pytest.mark.parametrize("receive_count", [1, 2])
def test_the_durable_lane_reruns_a_degraded_review_while_a_rerun_is_scheduled(
    monkeypatch, reason, receive_count,
) -> None:
    rerun, dispatch = _durable(monkeypatch, reason=reason, receive_count=receive_count)

    with pytest.raises(RuntimeError, match=reason):
        rerun._run_one(_job(), receive_count=receive_count)
    assert dispatch.call_args.kwargs["rerun_pending"] is True


@pytest.mark.parametrize("reason", sorted(_DEGRADED))
@pytest.mark.parametrize("receive_count", [3, 5])
def test_the_rerun_bound_still_caps_a_degraded_review(
    monkeypatch, reason, receive_count,
) -> None:
    """The bound is all_failed's: from the third delivery on, the degraded
    review publishes its failure and the job completes, no further re-run."""
    rerun, dispatch = _durable(monkeypatch, reason=reason, receive_count=receive_count)

    assert rerun._run_one(_job(), receive_count=receive_count) == "dispatched"
    assert dispatch.call_args.kwargs["rerun_pending"] is False
