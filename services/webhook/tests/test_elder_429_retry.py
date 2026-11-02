"""grug#1494: one provider 429 must not sink the whole Elder review.

Live 2026-10-28: the one cloud model answered 429, the chunk retry skipped
it as "already failed on this chunk", the free pool and the warm tier were
down, and every PR ended `all_failed` with a red check and no re-run.

1. A 429 is a "later", not a "no": the chunk retry waits out Retry-After and
   asks the same model again, and a later chunk is never skipped for it.
2. When every rung fails on a blocking repo, the durable lane re-runs the
   review a bounded number of times and the check reads pending (in_progress,
   so the required check still blocks the merge) instead of failure.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import llm_client as lc
import pytest
from llm_client import Backend, Hunk, LlmReviewResponse, review_diff
from personas.code_reviewer import dispatch as cr_dispatch
from review_evidence import assemble_evidence

from tests.test_elder_degraded_fail_closed import (
    _diff_response,
    _job,
    _patch_claims,
    _payload,
    _pr_data,
)


@pytest.fixture(autouse=True)
def _owned_ladder(monkeypatch):
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "test-ocg-key")
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    monkeypatch.delenv("GRUG_REVIEW_BACKEND_PRIORITY", raising=False)
    monkeypatch.delenv("GRUG_REVIEW_COHORT_CHARS", raising=False)


def _clean_review() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", "content": json.dumps({"findings": []})}},
            ],
            "model": "test-model-id",
        },
    )


def _rate_limited(retry_after: int) -> httpx.Response:
    return httpx.Response(
        429, headers={"Retry-After": str(retry_after)},
        json={"error": {"message": "rate limit exceeded"}},
    )


def test_a_429_on_the_first_pass_is_retried_after_the_delay_and_finishes(monkeypatch) -> None:
    """Every model answers 429 until the simulated clock passes Retry-After.
    The chunk retry waits that long, asks again, and the review completes
    instead of ending `all_failed` with every model "already failed"."""
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(lc, "_RETRY_SLEEP", sleep)
    calls: list[str] = []

    def respond(_url, **kwargs) -> httpx.Response:
        calls.append(kwargs["json"]["model"])
        return _rate_limited(20) if clock[0] < 20 else _clean_review()

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff(
            assemble_evidence([Hunk(path="src/x.py", body="@@ -1 +1 @@\n+x")]),
            installation_id=2,
        )

    assert out.kind == "reviewed", (out.kind, out.error, out.attempts)
    assert max(sleeps) >= 20, f"the retry did not wait out Retry-After: {sleeps}"
    assert calls[-1] in calls[:-1], "the rate-limited model was asked again"


def test_a_rate_limited_model_is_not_marked_failed_on_the_chunk() -> None:
    """The ladder's skip set holds models that really failed. A 429 is only a
    wait, so it stays out of the set while a timeout stays in it."""
    ladder = lc._ChunkLadder()
    seen: list[frozenset] = []

    def walk(outcomes):
        def call():
            seen.append(lc._LADDER_SKIP.get())
            trail = lc._LADDER_TRAIL.get()
            trail.extend(outcomes)
            return LlmReviewResponse(kind="all_failed", error="x")
        return call

    limited = lc._LadderAttempt(
        backend="poolside", model="m-limited", reason="http_429", retry_after=1.0,
    )
    broken = lc._LadderAttempt(backend="cave", model="m-broken", reason="ReadTimeout")
    with patch.object(lc, "_RETRY_SLEEP", lambda _s: None):
        ladder.run(0, walk([limited, broken]))
        ladder.run(0, walk([]))
        ladder.run(1, walk([]))

    assert seen[1] == frozenset({("cave", "m-broken")})
    assert seen[2] == frozenset(), "a later chunk starts with nothing skipped"


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
    monkeypatch.setattr(
        cr_dispatch, "review_diff",
        lambda *a, **kw: LlmReviewResponse(kind="all_failed", error="poolside: http_429"),
    )

    def run(*, blocking: bool, rerun_pending: bool):
        with patch("httpx.get", return_value=_diff_response()):
            result = cr_dispatch.dispatch_code_review(
                _payload(), blocking=blocking, rerun_pending=rerun_pending,
            )
        return result, posted

    return run


def test_all_rungs_failing_with_a_rerun_scheduled_reads_pending_when_blocking(elder) -> None:
    result, posted = elder(blocking=True, rerun_pending=True)

    assert result["degraded_reason"] == "all_failed"
    assert len(posted) == 1
    assert posted[0].status == "in_progress", "pending keeps the required check blocking"
    assert posted[0].conclusion is None
    assert "re-run" in posted[0].summary.lower()


def test_all_rungs_failing_with_no_rerun_left_still_fails_when_blocking(elder) -> None:
    _result, posted = elder(blocking=True, rerun_pending=False)

    assert [(c.status, c.conclusion) for c in posted] == [("completed", "failure")]


def test_all_rungs_failing_in_warn_mode_stays_neutral(elder) -> None:
    _result, posted = elder(blocking=False, rerun_pending=True)

    assert [(c.status, c.conclusion) for c in posted] == [("completed", "neutral")]


def _run_review_job(monkeypatch, *, receive_count: int) -> MagicMock:
    import rerun

    pr = _pr_data()
    fetches = iter((pr, pr))

    def token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")

    dispatch = MagicMock(return_value={
        "persona": "code_reviewer", "result": "skipped", "degraded_reason": "all_failed",
    })
    monkeypatch.setattr(rerun, "with_install_token_retry", token_retry)
    monkeypatch.setattr(rerun, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(
        rerun, "get_repo_config", lambda iid, rid: {"code_reviewer_blocking": True},
    )
    monkeypatch.setattr(rerun, "_set_board_in_progress_note", lambda *a: None)
    _patch_claims(monkeypatch)
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)
    with pytest.raises(RuntimeError, match="all_failed"):
        rerun._run_one(_job(), receive_count=receive_count)
    return dispatch


@pytest.mark.parametrize(
    "receive_count, pending", [(1, True), (2, True), (3, False), (5, False)],
)
def test_the_automatic_rerun_is_bounded(monkeypatch, receive_count, pending) -> None:
    """The queue redelivers a raised job. The first deliveries leave the check
    pending; the last ones publish the failure so it never waits forever."""
    dispatch = _run_review_job(monkeypatch, receive_count=receive_count)

    assert dispatch.call_args.kwargs["rerun_pending"] is pending
