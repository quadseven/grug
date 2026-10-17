"""grug#1216: the staged scheduler starves later cohorts.

Live 2026-10-17: the Tier-1 scheduler started a cohort only while
`elapsed + reserve <= budget`, with the reserve set to one worst-case ladder
walk (Cave review timeout + two SaaS fallbacks + every cloud tier: 480-575s
of a 600-700s budget). Staged and file-split reviews stopped starting cohorts
after 68-157s and ended `partial_review` with cohorts "never attempted", while
the review still had minutes left.

The fix runs the review under a deadline: every model call is clamped to the
time left, so the scheduler only reserves what one cohort needs.
"""
from __future__ import annotations

import json
import time
from unittest.mock import patch

import httpx
import llm_client as lc
import pytest
from llm_client import Backend, Hunk, LlmReviewResponse, review_diff


@pytest.fixture(autouse=True)
def _owned_ladder(monkeypatch):
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "test-ocg-key")
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda _s: None)
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    monkeypatch.setenv("GRUG_STAGED_REVIEW_BUDGET_S", "700")
    monkeypatch.setenv("GRUG_REVIEW_LLM_TIMEOUT_S", "330")
    monkeypatch.setenv("GRUG_REVIEW_COHORT_CHARS", "8000")
    monkeypatch.delenv("GRUG_REVIEW_BACKEND_PRIORITY", raising=False)


class _Clock:
    """Monotonic time that only moves when a model call says so."""

    def __init__(self) -> None:
        self.now = time.monotonic()

    def __call__(self) -> float:
        return self.now


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "model": "test-model-id",
        },
    )


def _big_hunk(path: str, marker: str) -> Hunk:
    body = "@@ -1 +1 @@\n" + "\n".join(f"+{marker} line {i}" for i in range(500))
    return Hunk(path=path, body=body)


def test_later_cohorts_still_run_while_the_budget_has_time(monkeypatch) -> None:
    """Three cohorts, each answered in 150s of a 700s budget. The old reserve
    (330 + 2 x 40 = 410s) skipped cohorts 2 and 3 after the first; under the
    deadline all three run and the review is complete."""
    clock = _Clock()
    monkeypatch.setattr(lc.time, "monotonic", clock)
    hunks = [
        _big_hunk("src/a.py", "AAA"),
        _big_hunk("src/b.py", "BBB"),
        _big_hunk("src/c.py", "CCC"),
    ]
    assert lc.review_is_staged(hunks)
    timeouts: list[float] = []

    def respond(_url, **kwargs) -> httpx.Response:
        timeouts.append(kwargs["timeout"])
        clock.now += 150.0
        return _completion(json.dumps({"findings": []}))

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff(hunks, installation_id=2)

    assert out.coverage is not None
    assert out.coverage.unattempted_cohorts == ()
    assert out.coverage.completed_cohorts == out.coverage.total_cohorts == 3
    assert out.kind == "reviewed", out.error
    # Every call was clamped to the deadline, never past it.
    assert all(t <= 700.0 for t in timeouts)
    assert timeouts[-1] <= 700.0 - 300.0


def test_a_call_is_clamped_to_the_time_left() -> None:
    sent: list[float] = []

    def respond(_url, **kwargs) -> httpx.Response:
        sent.append(kwargs["timeout"])
        return httpx.Response(200, json={})

    deadline = time.monotonic() + 30.0
    with patch.object(httpx, "post", side_effect=respond):
        lc._post_with_retries("http://x.test", {}, {}, 330.0, 1, 1, deadline)

    assert len(sent) == 1
    assert sent[0] <= 30.0


def test_a_call_is_not_sent_once_the_deadline_has_passed() -> None:
    with patch.object(httpx, "post") as post, pytest.raises(lc.ReviewDeadlineReached):
        lc._post_with_retries(
            "http://x.test", {}, {}, 330.0, 1, 1, time.monotonic() + 1.0,
        )
    post.assert_not_called()


def test_no_deadline_keeps_the_configured_timeout() -> None:
    sent: list[float] = []

    def respond(_url, **kwargs) -> httpx.Response:
        sent.append(kwargs["timeout"])
        return httpx.Response(200, json={})

    with patch.object(httpx, "post", side_effect=respond):
        lc._post_with_retries("http://x.test", {}, {}, 330.0, 1, 1)

    assert sent == [330.0]


def test_a_deadline_cut_timeout_does_not_slow_bench_the_model() -> None:
    """The clamp, not the model, made the call time out: re-raised as
    ReviewDeadlineReached, which the slow bench ignores."""
    def respond(_url, **_kwargs) -> httpx.Response:
        raise httpx.ReadTimeout("clamped")

    with (
        patch.object(httpx, "post", side_effect=respond),
        pytest.raises(lc.ReviewDeadlineReached) as cut,
    ):
        lc._post_with_retries(
            "http://x.test", {}, {}, 45.0, 1, 1, time.monotonic() + 20.0,
        )

    for _ in range(10):
        lc._note_model_timeout(Backend.POOLSIDE, lc._POOLSIDE_MODEL, cut.value)
    assert lc._slow_bench_state == {}


def test_a_full_timeout_under_a_distant_deadline_is_still_a_timeout() -> None:
    def respond(_url, **_kwargs) -> httpx.Response:
        raise httpx.ReadTimeout("slow model")

    with (
        patch.object(httpx, "post", side_effect=respond),
        pytest.raises(httpx.ReadTimeout),
    ):
        lc._post_with_retries(
            "http://x.test", {}, {}, 45.0, 1, 1, time.monotonic() + 600.0,
        )


def test_the_deadline_reaches_calls_inside_the_block_only() -> None:
    assert lc._REVIEW_DEADLINE.get() is None
    with lc._review_deadline(time.monotonic() + 100.0):
        outer = lc._REVIEW_DEADLINE.get()
        with lc._review_deadline(time.monotonic() + 500.0):
            assert lc._REVIEW_DEADLINE.get() == outer, "an inner block never extends"
    assert lc._REVIEW_DEADLINE.get() is None


def test_a_cohort_the_deadline_cut_is_reported_never_attempted() -> None:
    with lc._review_deadline(time.monotonic() + 5.0):
        responses = lc._run_staged_cohorts(
            cohort_count=1,
            run_cohort=lambda _index: LlmReviewResponse(
                kind="all_failed", error="poolside: ReviewDeadlineReached",
            ),
            budget_seconds=700,
            reserve_seconds=60,
            cancel_event=None,
            max_attempts=1,
        )

    assert responses[0].error.startswith(lc._SKIPPED_COHORT_PREFIX)
    assert lc._unattempted_cohort_indexes(responses) == (1,)


def test_a_cohort_that_really_failed_stays_failed() -> None:
    """Failed with time still on the deadline: a real failure, not a cut."""
    with lc._review_deadline(time.monotonic() + 600.0):
        responses = lc._run_staged_cohorts(
            cohort_count=1,
            run_cohort=lambda _index: LlmReviewResponse(
                kind="all_failed", error="poolside: ReadTimeout",
            ),
            budget_seconds=700,
            reserve_seconds=60,
            cancel_event=None,
            max_attempts=1,
        )

    assert responses[0].error == "poolside: ReadTimeout"
    assert lc._unattempted_cohort_indexes(responses) == ()


def test_a_cohort_cut_mid_ladder_by_the_deadline_is_never_attempted(monkeypatch) -> None:
    """Cohort 1 answers; cohort 2's models each run out their timeout. The
    deadline cuts its ladder, so it ends as budget-starved ("never
    attempted"), not as a model outage, and nothing runs past the budget."""
    clock = _Clock()
    started = clock.now
    monkeypatch.setattr(lc.time, "monotonic", clock)
    hunks = [_big_hunk("src/a.py", "AAA"), _big_hunk("src/b.py", "BBB")]

    def respond(_url, **kwargs) -> httpx.Response:
        prompt = kwargs["json"]["messages"][1]["content"]
        if "AAA" in prompt:
            clock.now += 100.0
            return _completion(json.dumps({"findings": []}))
        clock.now += kwargs["timeout"]
        raise httpx.ReadTimeout("slow")

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff(hunks, installation_id=2)

    assert out.coverage is not None
    assert out.coverage.unattempted_cohorts == (2,)
    assert out.coverage.completed_cohorts == 1
    # `_Tier1Review.started_at` reads the real clock a moment after `started`.
    assert clock.now - started <= 700.0 + 1.0
