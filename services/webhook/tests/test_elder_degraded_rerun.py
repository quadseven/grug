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
5. WARN mode waits too when a backend caused the degradation (#818).

#818 widened it again for the reasons the queue always re-runs
(`RETRIED_DEGRADATIONS`): those stay pending in BOTH modes, because a
completed "Grug could not review" put in front of the author while a retry
was already queued is the state #818 exists to stop reaching.
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
def test_a_backend_caused_degradation_in_warn_mode_reads_pending_while_a_rerun_is_owed(
    elder, reason,
) -> None:
    """#818: a pass degraded by failing backends publishes nothing
    author-visible while the queue still owes a re-run, in WARN mode too."""
    result, posted = elder(_DEGRADED[reason], blocking=False, rerun_pending=True)

    assert result.get("rerun_scheduled") == "true"
    assert [(c.status, c.conclusion) for c in posted] == [("in_progress", None)]


@pytest.mark.parametrize("reason", sorted(_DEGRADED))
def test_a_degraded_review_in_warn_mode_with_no_rerun_left_publishes_neutral(
    elder, reason,
) -> None:
    result, posted = elder(_DEGRADED[reason], blocking=False, rerun_pending=False)

    assert "rerun_scheduled" not in result
    assert [(c.status, c.conclusion) for c in posted] == [("completed", "neutral")]


def test_a_partial_review_not_caused_by_a_backend_is_not_held_in_warn_mode(elder) -> None:
    """A cohort the budget never walked is a big diff, not an outage: no
    re-run would repair it, so WARN mode publishes it at once."""
    budget_partial = LlmReviewResponse(
        kind="reviewed",
        error="partial review: cohorts [2] failed",
        backend_used=Backend.POOLSIDE,
        coverage=ReviewCoverage(
            total_cohorts=2, completed_cohorts=1, failed_cohorts=(2,),
            unattempted_cohorts=(2,), cohort_labels=("a", "b"),
        ),
    )
    result, posted = elder(budget_partial, blocking=False, rerun_pending=True)

    assert "rerun_scheduled" not in result
    assert [(c.status, c.conclusion) for c in posted] == [("completed", "neutral")]


@pytest.mark.parametrize("blocking", [True, False])
def test_a_degraded_pass_that_succeeds_on_the_rerun_never_shows_a_degraded_notice(
    elder, blocking,
) -> None:
    """First delivery degrades (pending), the re-run reviews cleanly: every
    check posted is pending or the clean verdict, none says degraded."""
    _first, posted = elder(_LOW_CONFIDENCE, blocking=blocking, rerun_pending=True)
    clean = LlmReviewResponse(kind="reviewed", backend_used=Backend.POOLSIDE)
    _second, posted = elder(clean, blocking=blocking, rerun_pending=False)

    assert [c.status for c in posted] == ["in_progress", "completed"]
    assert not any(c.summary.startswith("Degraded review") for c in posted)
    assert not any("could not review" in c.title.lower() for c in posted)


@pytest.mark.parametrize("blocking", [True, False])
def test_a_pass_that_degrades_on_every_attempt_publishes_the_notice_once_at_the_end(
    elder, blocking,
) -> None:
    posted = []
    for pending in (True, True, True, False):
        _result, posted = elder(_LOW_CONFIDENCE, blocking=blocking, rerun_pending=pending)

    assert [c.status for c in posted] == ["in_progress"] * 3 + ["completed"]
    notices = [c for c in posted if c.status == "completed"]
    assert len(notices) == 1
    assert notices[0].conclusion == ("failure" if blocking else "neutral")
    assert "Degraded review (low_confidence_review)" in notices[0].summary


def test_a_partial_review_with_a_blocking_finding_is_not_rerun(elder) -> None:
    """The blocking finding is a real verdict: it fails at once, the way a
    full review with that finding would, and nothing schedules a re-run."""
    result, posted = elder(_PARTIAL_WITH_BLOCKER, blocking=True, rerun_pending=True)

    assert "rerun_scheduled" not in result
    assert [(c.status, c.conclusion) for c in posted] == [("completed", "failure")]


@pytest.mark.parametrize("blocking", [True, False])
@pytest.mark.parametrize("reason", ["diff_too_large", "no_diff", "unknown_reason"])
def test_a_reason_outside_the_rerun_set_is_not_held_pending(reason, blocking) -> None:
    """Only the retried reasons and the two publishable degradations wait for
    a re-run. Any other degraded reason publishes as before."""
    evaluation = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason=reason,
    )

    assert cr_dispatch._hold_for_rerun(evaluation, blocking) is False


@pytest.mark.parametrize("blocking", [True, False])
@pytest.mark.parametrize("reason", sorted(cr_dispatch.RETRIED_DEGRADATIONS))
def test_a_retried_reason_is_held_pending_in_both_modes(reason, blocking) -> None:
    evaluation = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason=reason,
    )

    assert cr_dispatch._hold_for_rerun(evaluation, blocking) is True


@pytest.mark.parametrize("blocking", [True, False])
@pytest.mark.parametrize("kind", ["parse_failed", "all_failed"])
def test_a_retried_failure_reads_pending_while_a_rerun_is_scheduled(
    elder, kind, blocking,
) -> None:
    """#818: end to end, no completed check (neutral or failure) says Grug
    did not review while the queue still has a re-run for this pass."""
    result, posted = elder(
        LlmReviewResponse(kind=kind, error="prose, not JSON"),
        blocking=blocking, rerun_pending=True,
    )

    assert result["degraded_reason"] == kind
    assert result.get("rerun_scheduled") == "true"
    assert [(c.status, c.conclusion) for c in posted] == [("in_progress", None)]
    assert "could not review" not in posted[0].title.lower()
    merge_word = "blocked until" if blocking else "advisory"
    assert merge_word in posted[0].summary


def test_a_retried_failure_on_the_last_delivery_says_could_not_review(elder) -> None:
    """#818: once the retries are spent the pass says plainly that Grug did
    not review it, never that it did."""
    result, posted = elder(
        LlmReviewResponse(kind="parse_failed", error="prose, not JSON"),
        blocking=False, rerun_pending=False,
    )

    assert "rerun_scheduled" not in result
    assert [(c.status, c.conclusion) for c in posted] == [("completed", "neutral")]
    assert "Grug could not review this pass (parse_failed" in posted[0].title
    assert "an operator has been alerted" in posted[0].summary
    assert "Elder clear" not in posted[0].title


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
        if rerun_pending:
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
@pytest.mark.parametrize("receive_count", [1, 2, 3])
def test_the_durable_lane_reruns_a_degraded_review_while_a_rerun_is_scheduled(
    monkeypatch, reason, receive_count,
) -> None:
    rerun, dispatch = _durable(monkeypatch, reason=reason, receive_count=receive_count)

    with pytest.raises(RuntimeError, match=reason):
        rerun._run_one(_job(), receive_count=receive_count)
    assert dispatch.call_args.kwargs["rerun_pending"] is True


@pytest.mark.parametrize("reason", sorted(_DEGRADED))
@pytest.mark.parametrize("receive_count", [4, 5])
def test_the_rerun_bound_still_caps_a_degraded_review(
    monkeypatch, reason, receive_count,
) -> None:
    """The bound is all_failed's: from the fourth delivery on, the degraded
    review publishes its failure and the job completes, no further re-run."""
    rerun, dispatch = _durable(monkeypatch, reason=reason, receive_count=receive_count)

    assert rerun._run_one(_job(), receive_count=receive_count) == "dispatched"
    assert dispatch.call_args.kwargs["rerun_pending"] is False


# --- #818: a failed diff fetch, and the operator page ------------------------

@pytest.fixture
def fetch_fails(monkeypatch):
    """Drive `dispatch_code_review` with GitHub refusing the diff."""
    monkeypatch.setattr(cr_dispatch, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(
        cr_dispatch, "fetch_pr_diff_with_scope",
        MagicMock(side_effect=httpx.ConnectError("github down")),
    )
    monkeypatch.setattr(cr_dispatch, "record_check_verdict", lambda **kw: None)
    posted: list = []
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted.append(result) or {"id": 1}
        ),
    )

    def run(*, rerun_pending: bool):
        result = cr_dispatch.dispatch_code_review(
            _payload(), blocking=False, rerun_pending=rerun_pending,
        )
        return result, posted

    return run


def test_a_failed_diff_fetch_reads_pending_while_a_rerun_is_scheduled(
    fetch_fails, caplog,
) -> None:
    with caplog.at_level("ERROR"):
        result, posted = fetch_fails(rerun_pending=True)

    assert result["degraded_reason"] == "fetch_or_parse_failed"
    assert result.get("rerun_scheduled") == "true"
    assert [(c.status, c.conclusion) for c in posted] == [("in_progress", None)]
    assert "elder_could_not_review" not in caplog.text


def test_a_failed_diff_fetch_on_the_last_delivery_says_so_and_pages(
    fetch_fails, caplog,
) -> None:
    with caplog.at_level("ERROR"):
        result, posted = fetch_fails(rerun_pending=False)

    assert "rerun_scheduled" not in result
    assert [(c.status, c.conclusion) for c in posted] == [("completed", "neutral")]
    assert posted[0].title == (
        "Grug could not review this pass (fetch_or_parse_failed) - operator alerted"
    )
    pages = [r for r in caplog.records if r.getMessage() == "elder_could_not_review"]
    assert [(r.reason, r.surface) for r in pages] == [
        ("fetch_or_parse_failed", "fetch_or_parse"),
    ]


def test_a_blackout_check_pages_an_operator_before_it_publishes(elder, caplog) -> None:
    """#818: "an operator has been alerted" is never a claim with no alert."""
    with caplog.at_level("ERROR"):
        _result, posted = elder(
            LlmReviewResponse(kind="all_failed", error="poolside: http_503"),
            blocking=False, rerun_pending=False,
        )

    assert posted[-1].status == "completed"
    assert "operator alerted" in posted[-1].title
    pages = [r for r in caplog.records if r.getMessage() == "elder_could_not_review"]
    assert [(r.reason, r.surface) for r in pages] == [("all_failed", "dispatch")]


def test_a_partial_review_does_not_page(elder, caplog) -> None:
    """A partial review published real findings: no "could not review", no page."""
    with caplog.at_level("ERROR"):
        elder(_PARTIAL, blocking=False, rerun_pending=False)

    assert "elder_could_not_review" not in caplog.text
