"""Tests for the LLM client abstraction."""
from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import httpx
import pytest

import llm_client as lc
from llm_client import Backend, Finding, Hunk, LlmReviewResponse, review_diff
from personas.code_reviewer.persona import evaluate_diff
from review_pipeline import ReviewCohort, ReviewPlan


@pytest.fixture(autouse=True)
def _patch_keys(monkeypatch):
    """Avoid the real SSM round-trip and point review at the owned Cave.

    Review now runs the owned ensemble (coder + reasoner) via the spark-gateway;
    tests set GRUG_CAVE_GATEWAY_URL so _cave_review_config resolves. The SaaS key
    patches stay for the judge/select_backend paths that still use them."""
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "test-ocg-key")
    monkeypatch.delenv("GRUG_CLOUD_FREE_TIER_MODEL", raising=False)
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    # Fast = single (coder) arm; the deep tests below opt into both arms so a
    # second backend call cannot make every transport fixture run two reviews.
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")


def _hunk(path="src/x.py", body="@@ -1 +1 @@\n-foo\n+bar") -> Hunk:
    return Hunk(path=path, body=body)


def _openai_json_response(findings_json: str) -> dict:
    """OpenAI-compatible chat completion shape both backends return."""
    return {
        "choices": [
            {"message": {"content": findings_json, "role": "assistant"}},
        ],
        "model": "test-model-id",
    }


def test_round_robin_even_installation_picks_poolside() -> None:
    """installation_id % 2 == 0 → Poolside backend."""
    assert lc.select_backend(installation_id=2) == Backend.POOLSIDE
    assert lc.select_backend(installation_id=42) == Backend.POOLSIDE


def test_round_robin_odd_installation_picks_openrouter() -> None:
    assert lc.select_backend(installation_id=1) == Backend.OPENROUTER
    assert lc.select_backend(installation_id=43) == Backend.OPENROUTER


def test_empty_hunks_returns_no_diff_kind_without_llm_call() -> None:
    """Cheap short-circuit — don't burn LLM quota on empty diffs.
    Distinct `kind="no_diff"` so the caller can distinguish from
    `all_failed` (also has empty findings)."""
    with patch.object(httpx, "post") as mock_post:
        out = review_diff([], installation_id=1)
    assert out.kind == "no_diff"
    assert out.findings == ()
    assert out.backend_used is None
    mock_post.assert_not_called()


def test_review_diff_via_cave_coder_returns_structured_response() -> None:
    findings_json = (
        '{"findings": [{"path": "src/x.py", "line": 1, '
        '"rule": "secret-in-log", "severity": "high", '
        '"message": "API key in log"}]}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=2)

    assert isinstance(out, LlmReviewResponse)
    assert out.kind == "reviewed"
    # Fast mode returns after the first (coder) arm of the owned ensemble.
    assert out.backend_used == Backend.CAVE
    assert out.model_name == "test-model-id"
    assert len(out.findings) == 1
    assert isinstance(out.findings[0], Finding)
    assert out.findings[0].rule == "secret-in-log"
    assert out.findings[0].severity == "high"


def test_review_diff_empty_findings_returns_reviewed() -> None:
    findings_json = '{"findings": []}'
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.CAVE
    assert out.findings == ()


def test_large_review_runs_bounded_cohorts_and_merges_findings(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_COHORT_CHARS", "8000")
    annotate_calls = _capture_llmobs(monkeypatch)
    hunks = [
        _hunk("src/x.py", "@@ -1 +1 @@\n+SRC_CHANGE\n" + "s" * 4100),
        _hunk("tests/test_x.py", "@@ -1 +1 @@\n+TEST_CHANGE\n" + "t" * 4100),
    ]
    prompts: list[str] = []

    def respond(_url, **kwargs) -> httpx.Response:
        prompt = kwargs["json"]["messages"][1]["content"]
        prompts.append(prompt)
        if "SRC_CHANGE" in prompt:
            finding = (
                '{"path":"src/x.py","line":1,"rule":"src-bug",'
                '"severity":"high","message":"source bug"}'
            )
        else:
            finding = (
                '{"path":"tests/test_x.py","line":1,"rule":"test-bug",'
                '"severity":"medium","message":"test bug"}'
            )
        return httpx.Response(
            200,
            json=_openai_json_response(f'{{"findings":[{finding}]}}'),
        )

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff(
            hunks,
            installation_id=2,
            file_contents={
                "src/x.py": "SOURCE_FULL_FILE",
                "tests/test_x.py": "TEST_FULL_FILE",
            },
        )

    assert out.kind == "reviewed"
    assert {finding.rule for finding in out.findings} == {"src-bug", "test-bug"}
    assert len(prompts) == 2
    assert all("### REVIEW MAP" in prompt for prompt in prompts)
    source_prompt = next(prompt for prompt in prompts if "SRC_CHANGE" in prompt)
    test_prompt = next(prompt for prompt in prompts if "TEST_CHANGE" in prompt)
    assert "TEST_CHANGE" not in source_prompt
    assert "TEST_FULL_FILE" not in source_prompt
    assert "SRC_CHANGE" not in test_prompt
    assert "SOURCE_FULL_FILE" not in test_prompt
    assert {
        (
            call["tags"]["review_phase"],
            call["tags"]["cohort_index"],
            call["tags"]["cohort_count"],
        )
        for call in annotate_calls
    } == {("tier1", "1", "2"), ("tier1", "2", "2")}


def test_large_review_keeps_success_when_one_cohort_is_unparseable(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_COHORT_CHARS", "8000")
    hunks = [
        _hunk("src/x.py", "@@ -1 +1 @@\n+SRC_CHANGE\n" + "s" * 4100),
        _hunk("tests/test_x.py", "@@ -1 +1 @@\n+TEST_CHANGE\n" + "t" * 4100),
    ]

    def respond(_url, **kwargs) -> httpx.Response:
        prompt = kwargs["json"]["messages"][1]["content"]
        if "SRC_CHANGE" in prompt:
            return httpx.Response(200, json=_openai_json_response("not json"))
        return httpx.Response(
            200,
            json=_openai_json_response(
                '{"findings":[{"path":"tests/test_x.py","line":1,'
                '"rule":"test-bug","severity":"medium",'
                '"message":"test bug"}]}'
            ),
        )

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff(hunks, installation_id=2)

    assert out.kind == "reviewed"
    assert [finding.rule for finding in out.findings] == ["test-bug"]
    assert out.error == "partial review: cohorts [1] failed"
    assert out.coverage is not None
    assert out.coverage.total_cohorts == 2
    assert out.coverage.completed_cohorts == 1
    assert out.coverage.failed_cohorts == (1,)
    assert out.coverage.complete is False


def test_staged_scheduler_runs_one_cohort_at_a_time() -> None:
    active = 0
    max_active = 0
    order: list[int] = []

    def run(index: int) -> LlmReviewResponse:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        order.append(index)
        active -= 1
        return LlmReviewResponse(kind="all_failed", error=str(index))

    responses = lc._run_staged_cohorts(
        cohort_count=3,
        run_cohort=run,
        budget_seconds=700,
        reserve_seconds=100,
        cancel_event=None,
        # Isolate scheduling from retry - retry has its own tests below.
        max_attempts=1,
    )

    assert order == [0, 1, 2]
    assert max_active == 1
    assert len(responses) == 3


def test_staged_scheduler_marks_unstarted_cohorts_partial_when_budget_is_low() -> None:
    times = iter((0.0, 650.0))
    ran: list[int] = []

    responses = lc._run_staged_cohorts(
        cohort_count=3,
        run_cohort=lambda index: (
            ran.append(index)
            or LlmReviewResponse(kind="reviewed", backend_used=Backend.CAVE,
                                 model_name="coder")
        ),
        budget_seconds=700,
        reserve_seconds=100,
        cancel_event=None,
        clock=lambda: next(times),
    )

    assert ran == [0]
    assert len(responses) == 3
    assert responses[1].kind == "all_failed"
    assert responses[1].error == "cohort skipped: staged review budget exhausted"
    assert responses[2].error == "cohort skipped: staged review budget exhausted"


# --- #813/#707: cohorts dropped by max_cohorts BEFORE any of them ran ------
#
# `_run_staged_cohorts` (above) synthesizes explicit `all_failed` responses
# for cohorts skipped mid-run, so they correctly become `failed_indexes`.
# But `max_cohorts` truncation happens earlier, inside `plan_review` itself
# - those cohorts never reach `_run_staged_cohorts` at all, so they were
# invisible to `_merge_cohort_responses` until `ReviewPlan.total_cohorts_
# planned` existed to carry the true count through.


def _plan_cohort(label: str) -> ReviewCohort:
    return ReviewCohort(
        label=label, hunk_indexes=(0,), paths=(f"{label}.py",),
        diff_chars=10, oversized=False, layers=("implementation",),
    )


def test_merge_cohort_responses_flags_a_truncated_plan_as_partial() -> None:
    """2 cohorts ran and both succeeded, but the plan was for 5 - 3 were
    dropped by `max_cohorts` before they ever ran. Must still read as a
    partial review: MUST fail on main (error=="", coverage.complete==True)
    and pass after the fix."""
    plan = ReviewPlan(
        cohorts=(_plan_cohort("a"), _plan_cohort("b")),
        total_diff_chars=100,
        total_cohorts_planned=5,
    )
    responses = [
        LlmReviewResponse(
            kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
            model_name="m",
        ),
        LlmReviewResponse(
            kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
            model_name="m",
        ),
    ]

    merged = lc._merge_cohort_responses(responses, 1, None, plan)

    assert merged.kind == "reviewed"
    assert merged.error.startswith("partial review:"), (
        f"error={merged.error!r} - a truncated plan must read as partial"
    )
    assert "dropped before running" in merged.error
    assert merged.coverage is not None
    assert merged.coverage.total_cohorts == 5
    assert merged.coverage.complete is False
    assert merged.coverage.fraction == 2 / 5


def test_merge_cohort_responses_stays_clean_when_the_plan_was_not_truncated() -> None:
    plan = ReviewPlan(cohorts=(_plan_cohort("a"),), total_diff_chars=10)
    responses = [
        LlmReviewResponse(
            kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
            model_name="m",
        ),
    ]

    merged = lc._merge_cohort_responses(responses, 1, None, plan)

    assert merged.error == ""
    assert merged.coverage.complete is True
    assert merged.coverage.fraction == 1.0


def test_merge_cohort_responses_flags_a_truncated_cohort_as_partial() -> None:
    """grug#851: a cohort whose OWN generation hit the token cap
    (`_review_reasoner_diff_once` sets `error="partial review: ..."` via
    `_truncation_error`) still counts as `successful` for merge purposes -
    its findings are real, diff-anchored evidence - but the merge must not
    silently drop that on the floor and report a clean `error==""` just
    because every cohort technically "succeeded"."""
    plan = ReviewPlan(cohorts=(_plan_cohort("a"), _plan_cohort("b")), total_diff_chars=10)
    responses = [
        LlmReviewResponse(
            kind="reviewed", findings=(), backend_used=Backend.CAVE_REASONER,
            model_name="m",
            error="partial review: cave-reasoner hit the generation token "
            "cap before finishing (finish_reason=length)",
        ),
        LlmReviewResponse(
            kind="reviewed", findings=(), backend_used=Backend.CAVE_REASONER,
            model_name="m",
        ),
    ]

    merged = lc._merge_cohort_responses(responses, 1, None, plan)

    assert merged.kind == "reviewed"
    assert merged.error.startswith("partial review:"), (
        f"error={merged.error!r} - a truncated cohort must read as partial"
    )
    assert "token cap" in merged.error
    assert merged.coverage.complete is True  # both cohorts RAN and parsed


# --- cohort retry -----------------------------------------------------------
#
# A cohort used to get exactly one attempt: `responses.append(run_cohort(i))`.
# A transient backend blip or one unparseable completion was therefore
# permanent, and permanently poisoned the whole check via `partial_review`.


def _steady_clock(values):
    """Clock that yields `values` then holds the last one.

    A bare `iter(...)` raises StopIteration the moment the code under test
    reads the clock one more time than the test author predicted, which turns
    a behavior change into an unrelated-looking crash."""
    state = list(values)

    def now() -> float:
        return state.pop(0) if len(state) > 1 else state[0]

    return now


def test_transient_cohort_failure_is_retried_and_can_succeed() -> None:
    attempts: list[int] = []

    def run(index: int) -> LlmReviewResponse:
        attempts.append(index)
        if len(attempts) == 1:
            return LlmReviewResponse(kind="all_failed", error="backend blip")
        return LlmReviewResponse(
            kind="reviewed", backend_used=Backend.CAVE, model_name="coder",
        )

    responses = lc._run_staged_cohorts(
        cohort_count=1, run_cohort=run, budget_seconds=700,
        reserve_seconds=100, cancel_event=None,
    )

    assert attempts == [0, 0]
    assert responses[0].kind == "reviewed"


def test_cohort_retry_is_bounded_to_one_extra_attempt() -> None:
    attempts: list[int] = []

    def run(index: int) -> LlmReviewResponse:
        attempts.append(index)
        return LlmReviewResponse(kind="all_failed", error="still down")

    responses = lc._run_staged_cohorts(
        cohort_count=1, run_cohort=run, budget_seconds=700,
        reserve_seconds=100, cancel_event=None,
    )

    assert attempts == [0, 0]
    assert responses[0].kind == "all_failed"


def test_parse_failure_is_retried() -> None:
    """An unparseable completion is a re-roll candidate, not a verdict."""
    attempts: list[int] = []

    def run(index: int) -> LlmReviewResponse:
        attempts.append(index)
        return LlmReviewResponse(kind="parse_failed", error="bad json")

    lc._run_staged_cohorts(
        cohort_count=1, run_cohort=run, budget_seconds=700,
        reserve_seconds=100, cancel_event=None,
    )

    assert attempts == [0, 0]


def test_empty_cohort_is_not_retried() -> None:
    """`no_diff` is deterministic - re-asking cannot change the answer."""
    attempts: list[int] = []

    def run(index: int) -> LlmReviewResponse:
        attempts.append(index)
        return LlmReviewResponse(kind="no_diff")

    lc._run_staged_cohorts(
        cohort_count=1, run_cohort=run, budget_seconds=700,
        reserve_seconds=100, cancel_event=None,
    )

    assert attempts == [0]


def test_oversized_refusal_is_not_retried() -> None:
    """The refusal never called a model and depends only on the hunk size,
    so a second attempt burns budget to reach the identical answer."""
    attempts: list[int] = []

    def run(index: int) -> LlmReviewResponse:
        attempts.append(index)
        return lc._oversized_cohort_failure(
            lc.ReviewCohort(
                label="generated", hunk_indexes=(0,), paths=("big.json",),
                diff_chars=1_000_000, oversized=True, layers=("implementation",),
            ),
            phase="tier1", index=1, count=1, installation_id=1, pr_context=None,
        )

    lc._run_staged_cohorts(
        cohort_count=1, run_cohort=run, budget_seconds=700,
        reserve_seconds=100, cancel_event=None,
    )

    assert attempts == [0]


def test_retry_is_skipped_when_the_budget_cannot_afford_it() -> None:
    """A retry must not eat the time the remaining cohorts need."""
    attempts: list[int] = []

    def run(index: int) -> LlmReviewResponse:
        attempts.append(index)
        return LlmReviewResponse(kind="all_failed", error="down")

    responses = lc._run_staged_cohorts(
        cohort_count=2, run_cohort=run, budget_seconds=700,
        reserve_seconds=100, cancel_event=None,
        clock=_steady_clock([0.0, 650.0]),
    )

    assert attempts == [0]
    assert responses[1].error == "cohort skipped: staged review budget exhausted"


def test_retry_is_skipped_when_the_review_was_cancelled() -> None:
    import threading

    cancel = threading.Event()
    attempts: list[int] = []

    def run(index: int) -> LlmReviewResponse:
        attempts.append(index)
        cancel.set()
        return LlmReviewResponse(kind="all_failed", error="down")

    lc._run_staged_cohorts(
        cohort_count=1, run_cohort=run, budget_seconds=700,
        reserve_seconds=100, cancel_event=cancel,
    )

    assert attempts == [0]


def test_single_oversized_hunk_degrades_without_calling_model(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_COHORT_CHARS", "8000")

    with patch.object(httpx, "post") as mock_post:
        out = review_diff(
            [_hunk("src/generated.py", "@@ -1 +1 @@\n+x\n" + "x" * 8100)],
            installation_id=2,
        )

    assert out.kind == "all_failed"
    assert "hunk over the review budget" in out.error
    mock_post.assert_not_called()


def test_429_triggers_retry_with_backoff(monkeypatch) -> None:
    """A 429 (gateway under burst) is retried on the same arm before giving up."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)  # no real sleep
    seq = [
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        httpx.Response(200, json=_openai_json_response('{"findings":[]}')),
    ]
    idx = {"n": 0}

    def staged_post(*args, **kwargs):
        i = idx["n"]
        idx["n"] += 1
        return seq[i]

    with patch.object(httpx, "post", side_effect=staged_post):
        out = review_diff([_hunk()], installation_id=1)

    assert idx["n"] == 3, "should have made 3 attempts (2 retries after 429)"
    assert out.backend_used == Backend.CAVE


def test_coder_arm_failure_falls_back_to_reasoner_arm(monkeypatch) -> None:
    """5xx on the coder arm → no per-arm retry (might be permanent); fall back to
    the reasoner arm immediately. Both arms are owned (Cave gateway)."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    seq = [
        httpx.Response(500, json={"error": "upstream"}),  # coder arm
        httpx.Response(
            200,
            json=_openai_json_response(
                '{"findings": [{"rule": "x", "path": "p", "line": 1, '
                '"severity": "low", "message": "msg"}]}'
            ),
        ),  # reasoner arm
    ]
    idx = {"n": 0}

    def staged_post(*args, **kwargs):
        i = idx["n"]
        idx["n"] += 1
        return seq[i]

    with patch.object(httpx, "post", side_effect=staged_post):
        out = review_diff([_hunk()], installation_id=2)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.CAVE_REASONER
    assert len(out.findings) == 1


def test_cave_arms_carry_require_keys_json_schema(monkeypatch) -> None:
    """#609: both Cave arms must send the require-keys findings json_schema
    (extra_body replaces the default json_object, which ollama maps to a
    truncation-prone bare format=json - the #544 estate trap, production
    edition). The schema mirrors _coerce_finding's required fields."""
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")
    seen = []

    def respond(_url, **kwargs) -> httpx.Response:
        body = kwargs.get("json") or {}
        seen.append((body.get("model", ""), body.get("response_format", {})))
        return httpx.Response(200, json=_openai_json_response('{"findings": []}'))

    with patch.object(httpx, "post", side_effect=respond):
        review_diff([_hunk()], installation_id=1)

    assert len(seen) == 2
    for model, rf in seen:
        assert rf.get("type") == "json_schema", model
        schema = rf["json_schema"]["schema"]
        assert schema["required"] == ["findings"]
        assert schema["properties"]["findings"]["type"] == "array"
        item = schema["properties"]["findings"]["items"]
        assert item["required"] == ["path", "line", "rule", "severity", "message"]
        assert item["properties"]["severity"]["enum"] == [
            "low", "medium", "high", "critical",
        ]


def _is_reasoner(kwargs) -> bool:
    """True when this request targets the reasoner arm (Laguna-S-2.1), by inspecting
    the model in the outgoing body - both arms share the gateway URL now."""
    return "Laguna-S-2.1" in (kwargs.get("json") or {}).get("model", "")


def test_deep_review_consults_both_arms_and_merges_findings(monkeypatch) -> None:
    """A parseable empty first answer must not end a deep review. Both owned
    arms (coder + reasoner) run and their candidates are merged with source
    attribution for later human/judge evaluations."""
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")
    span_contexts = iter((
        {"trace_id": "coder-trace", "span_id": "coder-span"},
        {"trace_id": "reasoner-trace", "span_id": "reasoner-span"},
    ))
    monkeypatch.setattr(lc, "_llmobs_export", lambda span: next(span_contexts))

    def respond(url, **kwargs):
        if _is_reasoner(kwargs):
            content = (
                '{"findings": [{"path": "src/x.py", "line": 1, '
                '"rule": "null-deref", "severity": "high", '
                '"message": "unchecked optional"}]}'
            )
            model = "poolside/Laguna-S-2.1-NVFP4"
        else:
            content = '{"findings": []}'
            model = "qwen3-coder-next:q8_0"
        body = _openai_json_response(content)
        body["model"] = model
        return httpx.Response(200, json=body)

    with patch.object(httpx, "post", side_effect=respond) as mock_post:
        out = review_diff([_hunk()], installation_id=1)

    assert mock_post.call_count == 2
    assert out.kind == "reviewed"
    assert out.backends_used == (Backend.CAVE, Backend.CAVE_REASONER)
    assert out.models_used == (
        "qwen3-coder-next:q8_0", "poolside/Laguna-S-2.1-NVFP4",
    )
    assert len(out.findings) == 1
    assert out.findings[0].origins[0].backend == Backend.CAVE_REASONER
    assert out.findings[0].origins[0].review_span_context == {
        "trace_id": "reasoner-trace", "span_id": "reasoner-span",
    }


def test_deep_review_runs_both_arms_concurrently_not_sequentially(monkeypatch) -> None:
    """Arm parallelization: deep mode's two arms must overlap in wall-clock,
    not sum. Each mocked backend call sleeps 0.2s; sequential execution would
    take >=0.4s, concurrent execution should finish close to 0.2s. A generous
    upper bound (0.35s) absorbs scheduling/GIL-release jitter without letting
    a regression back to sequential silently pass."""
    import time

    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")

    def respond(url, **kwargs):
        time.sleep(0.2)
        content = '{"findings": []}'
        body = _openai_json_response(content)
        body["model"] = "poolside/Laguna-S-2.1-NVFP4" if _is_reasoner(kwargs) else "qwen3-coder-next:q8_0"
        return httpx.Response(200, json=body)

    with patch.object(httpx, "post", side_effect=respond) as mock_post:
        start = time.monotonic()
        out = review_diff([_hunk()], installation_id=1)
        elapsed = time.monotonic() - start

    assert mock_post.call_count == 2
    assert out.kind == "reviewed"
    assert elapsed < 0.35, f"expected concurrent arms (~0.2s), took {elapsed:.3f}s"


def test_call_backend_cancel_event_aborts_in_flight_request() -> None:
    """Mid-flight cancellation (#635 follow-up): a MOCKED httpx.post would
    never prove this, since a Python mock body ignores client.close()
    entirely - the watcher's cancellation has to interrupt a REAL blocked
    socket read. A local HTTP server holds the connection open for 5s
    before responding; cancel_event fires after ~0.3s. If _call_backend's
    watcher genuinely closes the client out from under the request, this
    returns in well under the 5s the server would otherwise hold it for."""
    import http.server
    import socketserver
    import threading
    import time

    class _SlowHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            time.sleep(5.0)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"{}"}}]}')

        def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature; quiet test output
            pass

    # ThreadingTCPServer (not TCPServer) + daemon_threads=True (FLINT,
    # #637): plain TCPServer.shutdown() blocks until the CURRENT request
    # finishes, so the abandoned 5s-sleeping handler would make every test
    # run pay close to the full 5s in teardown even though the assertions
    # above already passed. A threading server's shutdown() doesn't wait on
    # in-flight (daemon) request threads.
    class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    server = _ThreadingServer(("127.0.0.1", 0), _SlowHandler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        config = lc.BackendConfig(
            backend=Backend.CAVE,
            url=f"http://127.0.0.1:{port}/v1/chat/completions",
            model="test-model",
            key_loader=lambda: "test-key",
            timeout_seconds=10.0,
            retry_attempts=1,
        )
        cancel_event = threading.Event()
        threading.Thread(
            target=lambda: (time.sleep(0.3), cancel_event.set()), daemon=True,
        ).start()

        start = time.monotonic()
        with pytest.raises((httpx.RequestError, httpx.TimeoutException)):
            lc._call_backend(config, [{"role": "user", "content": "hi"}], cancel_event=cancel_event)
        elapsed = time.monotonic() - start
    finally:
        server.shutdown()
        server.server_close()

    assert elapsed < 2.0, f"expected cancellation within ~1s, took {elapsed:.2f}s (server holds for 5s)"


def test_call_backend_without_cancel_event_uses_plain_httpx_post() -> None:
    """Backward compat: callers that pass no cancel_event (the judge, the
    walkthrough summary, the SaaS fallback) keep hitting the module-level
    httpx.post - not a Client - so their existing test mocks (patch.object
    httpx, "post") keep working unmodified."""
    body = _openai_json_response('{"findings": []}')

    with patch.object(httpx, "post", return_value=httpx.Response(200, json=body)) as mock_post:
        config = lc.BackendConfig(
            backend=Backend.CAVE, url="http://cave.test/v1/chat/completions",
            model="test-model", key_loader=lambda: "test-key",
        )
        resp = lc._call_backend(config, [{"role": "user", "content": "hi"}])

    mock_post.assert_called_once()
    assert resp.status_code == 200


def test_deep_review_deduplicates_same_candidate_across_arms(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")
    content = (
        '{"findings": [{"path": "src/x.py", "line": 1, '
        '"rule": "null-deref", "severity": "high", "message": "bug"}]}'
    )
    response = httpx.Response(200, json=_openai_json_response(content))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert len(out.findings) == 1
    assert tuple(origin.backend for origin in out.findings[0].origins) == (
        Backend.CAVE, Backend.CAVE_REASONER,
    )


def test_deep_review_uses_stronger_duplicate_explanation(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")

    def respond(url, **kwargs):
        if _is_reasoner(kwargs):
            severity = "high"
            message = "the unchecked optional is dereferenced on this path"
        else:
            severity, message = "low", "maybe wrong"
        content = (
            '{"findings": [{"path": "src/x.py", "line": 1, '
            '"rule": "null-deref", '
            f'"severity": "{severity}", "message": "{message}"}}]}}'
        )
        return httpx.Response(200, json=_openai_json_response(content))

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff([_hunk()], installation_id=1)

    assert out.findings[0].severity == "high"
    assert out.findings[0].message == (
        "the unchecked optional is dereferenced on this path"
    )


def test_deep_review_one_arm_reply_is_a_complete_review(monkeypatch) -> None:
    # The two owned arms are best-effort: ONE reply is a complete review (never
    # provisional/retryable), so a reasoner-arm 402/5xx cannot block a review the
    # coder arm answered - `kind` stays `reviewed` and the findings publish.
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")

    def respond(url, **kwargs):
        if _is_reasoner(kwargs):
            return httpx.Response(402, json={"error": "Payment Required"})
        return httpx.Response(
            200,
            json=_openai_json_response(
                '{"findings": [{"path": "src/x.py", "line": 1, '
                '"rule": "lost-error", "severity": "high", "message": "bug"}]}'
            ),
        )

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    # grug#848: this used to assert `not out.error` ("one arm answering is
    # not an error"). Complete-enough-to-publish and complete are not the
    # same claim: the check-run must not vouch `success` for a look the
    # second arm never took, so the missing arm now reads as partial
    # coverage (the same seam PR #844 built) while the findings still post.
    assert out.error.startswith("partial review:")
    assert out.backends_used == (Backend.CAVE,)
    assert [finding.rule for finding in out.findings] == ["lost-error"]


def test_deep_review_one_arm_failure_routes_through_derive_conclusion(monkeypatch) -> None:
    """grug#848 (sibling of the tiered path): the concurrent dual-arm merge
    returned `kind="reviewed"`, `error=""` when only one of two arms
    succeeded, so `evaluate_diff` could not tell it from a clean two-arm
    pass and the check-run read `success` over a look the reasoner never
    took. Latent today (both deployments pin `GRUG_REVIEW_DEPTH=tiered`),
    which is exactly why it needs a test: a future flip to `deep` would
    reintroduce the bug wholesale.

    MUST fail on main (`out.error == ""`, `conclusion == "success"`) and
    pass once the missing arm is folded into the SAME `"partial review:"`
    -> `degraded_reason="partial_review"` -> `_derive_conclusion` seam
    PR #844 established for cohort coverage - no new vocabulary."""
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")

    def respond(url, **kwargs):
        if _is_reasoner(kwargs):
            raise httpx.ConnectError("reasoner down")
        # The emptiest, most "looks clean" reply - the one most likely to be
        # mistaken for a full two-arm pass.
        return httpx.Response(200, json=_openai_json_response('{"findings": []}'))

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backends_used == (Backend.CAVE,)
    assert out.error.startswith("partial review:"), (
        f"error={out.error!r} - one of two arms failed outright but the "
        "merge reads as a clean two-arm pass"
    )
    assert "cave-reasoner" in out.error

    evaluation = evaluate_diff((), out)
    assert evaluation.degraded_reason == "partial_review"
    assert evaluation.conclusion == "neutral", (
        f"conclusion={evaluation.conclusion!r} - a one-arm review must never "
        "surface as an unqualified success"
    )


def test_deep_review_both_arms_answering_is_not_partial(monkeypatch) -> None:
    """Guard against over-correction: two clean arms are a clean two-arm
    pass - `error` stays empty and the conclusion stays `success`."""
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")
    response = httpx.Response(200, json=_openai_json_response('{"findings": []}'))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.backends_used == (Backend.CAVE, Backend.CAVE_REASONER)
    assert out.error == ""
    assert evaluate_diff((), out).conclusion == "success"


def test_fast_review_reasoner_fallback_is_not_partial(monkeypatch) -> None:
    """`fast` is coder-first with the reasoner as FALLBACK, not a second
    arm: one reply is the whole design, so a coder failure rescued by the
    reasoner must not read as partial coverage. Only the concurrent
    dual-arm (`deep`) merge expects both arms to answer."""
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)

    def respond(url, **kwargs):
        if _is_reasoner(kwargs):
            return httpx.Response(200, json=_openai_json_response('{"findings": []}'))
        return httpx.Response(500, json={"error": "upstream"})

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backends_used == (Backend.CAVE_REASONER,)
    assert out.error == ""


def test_review_depth_defaults_to_tiered_single_arm(monkeypatch) -> None:
    """Unset depth = tiered: ordinary small diff spends only the coder arm."""
    monkeypatch.delenv("GRUG_REVIEW_DEPTH", raising=False)
    monkeypatch.setenv("GRUG_DEEP_SAMPLE_RATE", "0")
    monkeypatch.setenv("GRUG_DEEP_DIFF_LINES", "99999")
    response = httpx.Response(200, json=_openai_json_response('{"findings": []}'))

    with patch.object(httpx, "post", return_value=response) as post:
        out = review_diff([_hunk()], installation_id=1)

    assert post.call_count == 1
    assert out.backends_used == (Backend.CAVE,)


def test_review_depth_deep_still_runs_both_arms(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")
    response = httpx.Response(200, json=_openai_json_response('{"findings": []}'))

    with patch.object(httpx, "post", return_value=response) as post:
        out = review_diff([_hunk()], installation_id=1)

    assert post.call_count == 2
    assert out.backends_used == (Backend.CAVE, Backend.CAVE_REASONER)


def test_decide_deep_escalation_diff_lines() -> None:
    body = "@@ -1 +1 @@\n" + "\n".join(f"+line{i}" for i in range(10))
    decision = lc.decide_deep_escalation(
        [_hunk(body=body)],
        sample_rate=0.0,
        diff_line_threshold=5,
        path_markers=(),
    )
    assert decision.escalate is True
    assert decision.added_lines == 10
    assert any(r.startswith("diff_lines:") for r in decision.reasons)


def test_decide_deep_escalation_diff_lines_exclusive_bound() -> None:
    """Threshold N means above N, not at exactly N (GRUG_DEEP_DIFF_LINES=500)."""
    at = "@@ -1 +1 @@\n" + "\n".join(f"+line{i}" for i in range(5))
    over = "@@ -1 +1 @@\n" + "\n".join(f"+line{i}" for i in range(6))
    at_bound = lc.decide_deep_escalation(
        [_hunk(body=at)], sample_rate=0.0, diff_line_threshold=5, path_markers=(),
    )
    above = lc.decide_deep_escalation(
        [_hunk(body=over)], sample_rate=0.0, diff_line_threshold=5, path_markers=(),
    )
    assert at_bound.escalate is False
    assert above.escalate is True


def test_decide_deep_escalation_high_risk_path() -> None:
    decision = lc.decide_deep_escalation(
        [_hunk(path="services/auth/login.py")],
        sample_rate=0.0,
        diff_line_threshold=99999,
        path_markers=("auth",),
    )
    assert decision.escalate is True
    assert any(r.startswith("high_risk_paths:") for r in decision.reasons)


def test_decide_deep_escalation_explicit_marker() -> None:
    decision = lc.decide_deep_escalation(
        [_hunk()],
        pr_context={"title": "please deep-review this", "body": ""},
        sample_rate=0.0,
        diff_line_threshold=99999,
        path_markers=(),
    )
    assert decision.escalate is True
    assert "explicit_deep_review" in decision.reasons


def test_decide_deep_escalation_sample_is_deterministic() -> None:
    ctx = {"repo": "o/r", "pr_number": 7, "head_sha": "abc123"}
    a = lc.decide_deep_escalation(
        [_hunk()], pr_context=ctx, sample_rate=1.0,
        diff_line_threshold=99999, path_markers=(),
    )
    b = lc.decide_deep_escalation(
        [_hunk()], pr_context=ctx, sample_rate=1.0,
        diff_line_threshold=99999, path_markers=(),
    )
    assert a.escalate is True and b.escalate is True
    none = lc.decide_deep_escalation(
        [_hunk()], pr_context=ctx, sample_rate=0.0,
        diff_line_threshold=99999, path_markers=(),
    )
    assert none.escalate is False


def test_tiered_risky_path_stays_coder_only_for_required_path(monkeypatch) -> None:
    """#646: tiered never waits on reasoner inside review_diff (async append)."""
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "tiered")
    monkeypatch.setenv("GRUG_DEEP_SAMPLE_RATE", "0")
    monkeypatch.setenv("GRUG_DEEP_DIFF_LINES", "99999")
    response = httpx.Response(200, json=_openai_json_response('{"findings": []}'))

    with patch.object(httpx, "post", return_value=response) as post:
        out = review_diff(
            [_hunk(path="pkg/crypto/keys.py")],
            installation_id=1,
        )

    assert post.call_count == 1
    assert out.backends_used == (Backend.CAVE,)


def test_review_reasoner_diff_runs_only_reasoner_arm(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    response = httpx.Response(200, json=_openai_json_response('{"findings": []}'))

    with patch.object(httpx, "post", return_value=response) as post:
        out = lc.review_reasoner_diff([_hunk()], installation_id=1)

    assert post.call_count == 1
    assert out.kind == "reviewed"
    assert out.backends_used == (Backend.CAVE_REASONER,)


def test_cave_reasoner_has_server_side_completion_budget() -> None:
    """A timed-out client must not leave Laguna generating indefinitely."""
    reasoner = lc._cave_review_config(Backend.CAVE_REASONER)
    coder = lc._cave_review_config(Backend.CAVE)

    assert reasoner is not None
    assert reasoner.extra_body["max_tokens"] == 6_144
    assert coder is not None
    assert "max_tokens" not in coder.extra_body


def test_cave_reasoner_disables_default_thinking_like_the_judge() -> None:
    """grug#851: the judge (_cave_judge_config) disables Laguna's default
    long-form reasoning on this SAME model because it "turned this small call
    into a five-minute constrained-decoding pass and triggered xgrammar FSM
    errors live" - that mitigation was applied to the judge and never to the
    reasoner discovery arm. Under the reasoner's hard max_tokens cap plus its
    schema-constrained decoder, unchecked thinking can consume the whole
    budget before a single finding is written, and the decoder can legally
    close out valid-but-empty JSON when it runs out of room - a silent,
    indistinguishable-from-clean failure. Must match the judge's config."""
    reasoner = lc._cave_review_config(Backend.CAVE_REASONER)
    judge = lc._cave_judge_config()

    assert reasoner is not None
    assert judge is not None
    assert reasoner.extra_body["chat_template_kwargs"] == {"enable_thinking": False}
    assert reasoner.extra_body["chat_template_kwargs"] == judge.extra_body["chat_template_kwargs"]
    # The coder arm's decode budget is short enough that this mitigation was
    # never needed there; scoping the assertion to the reasoner only.
    coder = lc._cave_review_config(Backend.CAVE)
    assert coder is not None
    assert "chat_template_kwargs" not in coder.extra_body


def test_review_reasoner_diff_truncated_generation_is_not_a_clean_pass(monkeypatch) -> None:
    """grug#851: `finish_reason` is never read anywhere in `services/` on
    main - a generation cut off at the reasoner's max_tokens cap parses as a
    complete, clean review (a schema-constrained decoder that runs out of
    budget mid-thought can legally close out `{"findings": []}`). That must
    not be indistinguishable from a genuinely clean pass: the check-run
    conclusion must never read "Elder clear - no markings" with `success`
    over ground the reasoner never actually walked.

    MUST fail on main (`out.error == ""`, `evaluation.conclusion ==
    "success"`) and pass after the fix (`out.error` starts with
    `"partial review:"`, `evaluation.conclusion == "neutral"`) - routed
    through the SAME `degraded_reason`/`_derive_conclusion` seam PR #844
    built for cohort partial coverage, not a new vocabulary term.
    """
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    body = _openai_json_response('{"findings": []}')
    body["choices"][0]["finish_reason"] = "length"
    response = httpx.Response(200, json=body)

    with patch.object(httpx, "post", return_value=response):
        out = lc.review_reasoner_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.findings == ()
    assert out.error.startswith("partial review:"), (
        f"error={out.error!r} - a truncated (finish_reason=length) "
        "generation must read as partial, never as a silent clean pass"
    )

    # Full round trip through the persona layer: no findings and no real
    # hunks were needed for the anti-hallucination filter here (findings is
    # already empty), so an empty hunks tuple isolates the assertion to the
    # exact thing #851 broke - conclusion must never be "success".
    evaluation = evaluate_diff((), out)
    assert evaluation.degraded_reason == "partial_review"
    assert evaluation.conclusion == "neutral", (
        f"conclusion={evaluation.conclusion!r} - a truncated generation must "
        'never surface as "Elder clear - no markings" with a success '
        "conclusion"
    )


def test_openrouter_review_uses_opus_with_high_adaptive_reasoning() -> None:
    config = lc._review_backend_config(Backend.OPENROUTER)
    assert config.model == "anthropic/claude-opus-4.7"
    assert config.extra_body["reasoning"] == {"effort": "high", "exclude": True}
    assert config.extra_body["max_tokens"] == 32_768
    # Shared callers such as Teller and the judge remain on the cheap config.
    shared = lc._BACKEND_CONFIGS[Backend.OPENROUTER]
    assert shared.model == "anthropic/claude-haiku-4.5"
    assert "reasoning" not in shared.extra_body


def test_both_backends_fail_returns_all_failed_kind() -> None:
    """Distinct `kind="all_failed"` so the caller can switch on it
    without colliding with `no_diff`."""
    response = httpx.Response(500, json={"error": "down"})

    with patch.object(httpx, "post", return_value=response), \
         patch.object(lc, "_RETRY_SLEEP", lambda s: None):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "all_failed"
    assert out.findings == ()
    assert out.backend_used is None
    assert out.error  # non-empty


def test_timeout_treated_as_failure(monkeypatch) -> None:
    """A timeout (httpx.ReadTimeout) on the coder arm should fall back to the
    reasoner arm, not crash the webhook. Both arms share the gateway URL, so the
    mock dispatches on the model in the outgoing body."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    call_log: list = []
    success = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    def staged(url, *args, **kwargs):
        model = (kwargs.get("json") or {}).get("model", "")
        call_log.append(model)
        # Coder arm times out; the reasoner arm answers.
        if "Laguna-S-2.1" in model:
            return success
        raise httpx.ReadTimeout("timeout")

    with patch.object(httpx, "post", side_effect=staged):
        out = review_diff([_hunk()], installation_id=2)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.CAVE_REASONER
    assert any("Laguna-S-2.1" in m for m in call_log)


def test_request_uses_openai_chat_completions_shape() -> None:
    captured: list = []

    def capture(url, *, json, headers, timeout):
        captured.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    with patch.object(httpx, "post", side_effect=capture):
        review_diff([_hunk()], installation_id=1)

    assert len(captured) == 1
    body = captured[0]["json"]
    assert "model" in body
    assert isinstance(body["messages"], list)
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    # #609: Cave arms carry the require-keys findings json_schema (extra_body
    # replaces the default json_object - the truncation-prone bare format=json).
    assert body.get("response_format", {}).get("type") == "json_schema"
    # Authorization header carries the loaded key (in-cluster placeholder).
    assert captured[0]["headers"]["Authorization"].startswith("Bearer ")
    # Review gets a multi-minute read budget.
    assert captured[0]["timeout"] == lc._DEFAULT_REVIEW_TIMEOUT_SECONDS


def test_review_llm_timeout_default_covers_measured_slow_pass() -> None:
    """The default per-arm budget must clear the ~318s reasoner pass measured
    live on 2026-07-13 (the old 150s value made every big-diff review degrade
    to all_failed), while two sequential arms still fit inside the 720s
    durable-job deadline from #623."""
    import consumer

    assert lc._review_llm_timeout_s() == 330.0
    assert lc._DEFAULT_REVIEW_TIMEOUT_SECONDS > 318
    # Compare against the REAL durable-job budget, not a hard-coded 720, so
    # the hierarchy assertion tracks consumer.py if the deadline ever moves
    # (FLINT on #625).
    assert 2 * lc._MAX_REVIEW_TIMEOUT_SECONDS < consumer._review_job_timeout_s()


def test_review_llm_timeout_env_override(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_LLM_TIMEOUT_S", "200")
    assert lc._review_llm_timeout_s() == 200.0


def test_review_llm_timeout_clamps_to_deadline_hierarchy(monkeypatch) -> None:
    """Values that would break 2 x arm < GRUG_REVIEW_JOB_TIMEOUT_S clamp to
    the ceiling; absurdly small values clamp to the floor."""
    monkeypatch.setenv("GRUG_REVIEW_LLM_TIMEOUT_S", "10000")
    assert lc._review_llm_timeout_s() == lc._MAX_REVIEW_TIMEOUT_SECONDS
    monkeypatch.setenv("GRUG_REVIEW_LLM_TIMEOUT_S", "1")
    assert lc._review_llm_timeout_s() == lc._MIN_REVIEW_TIMEOUT_SECONDS


def test_review_llm_timeout_invalid_value_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_LLM_TIMEOUT_S", "not-a-number")
    assert lc._review_llm_timeout_s() == lc._DEFAULT_REVIEW_TIMEOUT_SECONDS


def test_cave_calls_are_tagged_interactive_priority(monkeypatch) -> None:
    """Grug's own review ensemble (coder + reasoner arms) carries
    X-Spark-Priority: interactive so the spark-gateway priority queue
    (quadseven/infra#1768) lets it jump ahead of Hermes's long agentic turns
    on the same shared, single-generation-slot Ollama target - the exact
    2026-07-12 incident this header exists to prevent. Deep depth so BOTH
    arms fire (see test_deep_review_consults_both_arms_and_merges_findings)
    - a coder-only run would pass even if the reasoner arm's config lost
    the header."""
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")
    captured: list = []

    def capture(_url, *, headers, **_kwargs: object) -> httpx.Response:
        captured.append(headers)
        return httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    with patch.object(httpx, "post", side_effect=capture):
        review_diff([_hunk()], installation_id=1)

    assert len(captured) == 2
    assert all(h.get("X-Spark-Priority") == "interactive" for h in captured)


def test_cave_calls_carry_per_arm_caller_attribution(monkeypatch) -> None:
    """X-Spark-Caller (2026-07-14 fix): grug's Elder review was the one
    production caller with NO caller attribution at all, despite being the
    highest-volume consumer - the gateway dashboard's `source` tag fell back
    to a pod-IP guess for every single one of its requests. Distinguishes
    coder vs reasoner so the dashboard can tell them apart too."""
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")
    captured: list = []

    def capture(_url, *, json, headers, **_kwargs: object) -> httpx.Response:
        captured.append((json.get("model", ""), headers.get("X-Spark-Caller")))
        return httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    with patch.object(httpx, "post", side_effect=capture):
        review_diff([_hunk()], installation_id=1)

    assert len(captured) == 2
    callers_by_model = dict(captured)
    assert callers_by_model["qwen3-coder-next:q8_0"] == "grug-elder-coder"
    assert callers_by_model["poolside/Laguna-S-2.1-NVFP4"] == "grug-elder-reasoner"


def test_extra_headers_cannot_override_authorization(monkeypatch) -> None:
    """FLINT #618: extra_headers is caller-controlled config, not user
    input, but a future backend accidentally setting Authorization in it
    (any case) must not silently replace the real bearer token - fail loud
    instead."""
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    config = lc.BackendConfig(
        backend=Backend.POOLSIDE,
        url="http://example.test/v1/chat/completions",
        model="m",
        key_loader=lambda: "test-pool-key",
        extra_headers={"authorization": "Bearer evil"},
    )
    with pytest.raises(lc._BackendConfigError, match="must not contain Authorization"):
        lc._call_backend(config, messages=[{"role": "user", "content": "hi"}])


def test_malformed_llm_json_returns_parse_failed_kind() -> None:
    """LLM occasionally returns prose around the JSON or just refuses
    to comply. Don't crash the webhook on a parse error — discriminated
    `kind="parse_failed"` so the caller posts an advisory check-run
    explaining the issue rather than silent "no findings"."""
    response = httpx.Response(200, json=_openai_json_response("sorry, I cannot do that"))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "parse_failed"
    assert out.findings == ()
    assert out.backend_used == Backend.CAVE
    assert "parse" in out.error.lower()


def test_findings_with_bogus_severity_are_dropped() -> None:
    """A hallucinating LLM might return severity='catastrophic' which
    isn't in the Literal. Drop the malformed entry rather than
    iterating over `Any` downstream."""
    findings_json = (
        '{"findings": ['
        '{"path": "x", "line": 1, "rule": "ok", "severity": "high", "message": ""},'
        '{"path": "y", "line": 2, "rule": "bad", "severity": "catastrophic", "message": ""},'
        '{"path": "z", "line": 3, "rule": "also-bad", "severity": "low", "message": ""}'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    # Bogus-severity entry dropped; valid two remain.
    assert len(out.findings) == 2
    assert {f.rule for f in out.findings} == {"ok", "also-bad"}


def test_findings_with_missing_fields_are_dropped() -> None:
    """LLM omitting a required field (e.g. `line`) → drop the entry."""
    findings_json = (
        '{"findings": ['
        '{"path": "x", "rule": "no-line", "severity": "high"},'  # missing line
        '{"path": "y", "line": 5, "rule": "ok", "severity": "low", "message": ""}'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert len(out.findings) == 1
    assert out.findings[0].rule == "ok"


def test_unconfigured_cave_gateway_falls_back_to_saas(monkeypatch) -> None:
    """No GRUG_CAVE_GATEWAY_URL → both ensemble arms are misconfigured, no
    HTTP call made for either (the guard runs before the span) → but Cave
    produced nothing usable, so the OpenRouter/Poolside overload fallback
    still gets a shot rather than leaving the review all_failed outright."""
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL", raising=False)
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    success = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    with patch.object(httpx, "post", return_value=success) as mock_post:
        out = review_diff([_hunk()], installation_id=1)
    assert out.kind == "reviewed"
    assert out.backend_used == Backend.POOLSIDE
    mock_post.assert_called_once()  # Cave never dialed; only the fallback


def test_unconfigured_cave_gateway_and_saas_down_returns_all_failed(monkeypatch) -> None:
    """Same as above, but the overload fallback ALSO fails - still degrades
    cleanly to all_failed rather than crashing the webhook handler."""
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL", raising=False)
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("down")) as mock_post:
        out = review_diff([_hunk()], installation_id=1)
    assert out.kind == "all_failed"
    # last_error reflects the LAST attempt (OpenRouter's transport failure) -
    # Cave's earlier misconfiguration is superseded, not lost (both are
    # logged individually via llm_backend_misconfigured).
    assert "connecterror" in out.error.lower()
    # Cave never dialed (config guard runs before the span); only the two
    # single-shot overload-fallback attempts (Poolside, OpenRouter).
    assert mock_post.call_count == 2


def test_503_retried_alongside_429(monkeypatch) -> None:
    """503 is routinely transient on CF edge; retry once before falling
    back. Previous behavior burned the whole backend on a 1-second blip."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    seq = [
        httpx.Response(503, json={"error": "service unavailable"}),
        httpx.Response(200, json=_openai_json_response('{"findings":[]}')),
    ]
    idx = {"n": 0}

    def staged_post(*args, **kwargs):
        i = idx["n"]
        idx["n"] += 1
        return seq[i]

    with patch.object(httpx, "post", side_effect=staged_post):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.CAVE
    assert idx["n"] == 2  # one retry + one success


def test_transport_failure_on_both_backends_returns_all_failed(monkeypatch) -> None:
    """Covers the retry-loop terminal `raise` (final attempt without a
    fallback continue). Without this test, a future off-by-one on the
    `attempt < _RETRY_ATTEMPTS - 1` guard would ship green."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    call_log: list[str] = []

    def always_timeout(url, *args, **kwargs):
        call_log.append((kwargs.get("json") or {}).get("model", ""))
        raise httpx.ReadTimeout("timeout")

    with patch.object(httpx, "post", side_effect=always_timeout):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "all_failed"
    assert out.backend_used is None
    # Long review timeouts are not retried; one attempt per arm bounds the deep
    # generation phase even though quick 429/503 responses still retry. Both
    # Cave arms fail (2) -> the OpenRouter/Poolside overload fallback also
    # gets one single-shot attempt each (2) since Cave produced nothing
    # usable = 4 total.
    assert len(call_log) == 4
    # Both Cave arms represented (coder + reasoner models). Assert the coder
    # substring explicitly: "qwen" alone also matches the reasoner
    # (Laguna-S-2.1), so it could pass on two reasoner calls.
    assert any("Laguna-S-2.1" in m for m in call_log)
    assert any("qwen3-coder" in m for m in call_log)
    # The overload fallback tier also fired, in order, after both Cave arms -
    # each backend's fast default model (not the Opus review override).
    assert call_log[2:] == [lc._POOLSIDE_MODEL, lc._OPENROUTER_MODEL]


def test_review_diff_skips_saas_fallback_when_cancelled(monkeypatch) -> None:
    """Mid-flight cancellation (#635 follow-up): when both Cave arms fail
    because cancel_event was already set, review_diff must return
    all_failed WITHOUT trying OpenRouter/Poolside - that would burn a real
    SaaS call chasing a snapshot the pre-publish freshness check is about
    to discard anyway. cancel_event pre-set makes _call_backend raise
    before any network call, so the mock should never fire at all."""
    import threading

    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")
    cancel_event = threading.Event()
    cancel_event.set()

    with patch.object(httpx, "post") as mock_post:
        out = review_diff([_hunk()], installation_id=1, cancel_event=cancel_event)

    mock_post.assert_not_called()
    assert out.kind == "all_failed"
    # #773: the watcher also fires on a title/body edit, so the message must
    # not name a commit that may never have happened.
    assert out.error == "cancelled: review input changed while the review was running"


def test_saas_overload_fallback_rescues_review_when_cave_fully_down(monkeypatch) -> None:
    """The operator's 2026-07-14 call: when both Cave arms are unreachable (the
    Sparks/spark-gateway overloaded), OpenRouter/Poolside step in as a
    last-resort so the review still completes instead of going all_failed."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)

    def respond(url, **kwargs):
        model = (kwargs.get("json") or {}).get("model", "")
        if model in ("qwen3-coder-next:q8_0", "poolside/Laguna-S-2.1-NVFP4"):
            raise httpx.ConnectTimeout("cave overloaded")
        assert model == lc._POOLSIDE_MODEL  # Poolside tried before OpenRouter
        return httpx.Response(
            200,
            json=_openai_json_response(
                '{"findings": [{"path": "src/x.py", "line": 1, '
                '"rule": "lost-error", "severity": "high", "message": "bug"}]}'
            ),
        )

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.POOLSIDE
    assert out.backends_used == (Backend.POOLSIDE,)
    assert [finding.rule for finding in out.findings] == ["lost-error"]


def test_saas_overload_fallback_tries_openrouter_after_poolside_fails(monkeypatch) -> None:
    """Poolside also down -> OpenRouter gets a shot before giving up."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)

    def respond(url, **kwargs):
        model = (kwargs.get("json") or {}).get("model", "")
        if model == lc._OPENROUTER_MODEL:
            return httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
        raise httpx.ConnectTimeout("down")

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.OPENROUTER


def test_saas_overload_fallback_uses_fast_default_model_not_review_opus(monkeypatch) -> None:
    """The fallback tier must NOT inherit the Opus-plus-high-reasoning review
    override - that config is tuned for a multi-minute quality pass and would
    blow the tier's tight reserved time budget. It gets each backend's fast,
    low-latency shared-config default instead."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    captured: list[dict] = []

    def respond(url, **kwargs):
        model = (kwargs.get("json") or {}).get("model", "")
        if model in ("qwen3-coder-next:q8_0", "poolside/Laguna-S-2.1-NVFP4"):
            raise httpx.ConnectTimeout("cave overloaded")
        captured.append(kwargs)
        return httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    with patch.object(httpx, "post", side_effect=respond):
        review_diff([_hunk()], installation_id=1)

    assert captured[0]["json"]["model"] == lc._POOLSIDE_MODEL
    assert captured[0]["json"]["model"] != lc._OPENROUTER_REVIEW_MODEL
    assert captured[0]["timeout"] == lc._SAAS_OVERLOAD_FALLBACK_TIMEOUT_SECONDS


def test_saas_overload_fallback_never_engages_when_a_cave_arm_succeeds(monkeypatch) -> None:
    """The fallback tier is a last resort, not a race - it must not fire at
    all when Cave itself produced a usable review."""
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    call_log: list[str] = []

    def respond(url, **kwargs):
        model = (kwargs.get("json") or {}).get("model", "")
        call_log.append(model)
        return httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.CAVE
    assert len(call_log) == 1  # fast mode short-circuits on the first success
    assert lc._POOLSIDE_MODEL not in call_log
    assert lc._OPENROUTER_MODEL not in call_log


def test_saas_overload_fallback_skipped_when_cave_returns_parse_failed(monkeypatch) -> None:
    """A Cave arm that DID respond but unparseably is a model/prompt bug, not
    overload - the fallback must not engage (retrying on SaaS would not fix
    a prompt/parsing issue) and parse_failed must still win over all_failed."""
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")
    call_log: list[str] = []

    def respond(url, **kwargs):
        model = (kwargs.get("json") or {}).get("model", "")
        call_log.append(model)
        return httpx.Response(200, json=_openai_json_response("not json"))

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "parse_failed"
    assert len(call_log) == 2  # both Cave arms only, no SaaS fallback
    assert lc._POOLSIDE_MODEL not in call_log
    assert lc._OPENROUTER_MODEL not in call_log


# --- OpenRouter free-tier rate limiter gate (grug#870, epic #869) ----------


def _with_openrouter_model(monkeypatch, model: str) -> None:
    """Point the shared OpenRouter BackendConfig at `model` (the review
    override, the SaaS-overload fallback, Teller, ask, and the judge all
    derive from this ONE dict entry via `replace()`), same trick
    `test_openrouter_review_uses_opus_with_high_adaptive_reasoning`
    inspects the read side of."""
    monkeypatch.setattr(
        lc,
        "_BACKEND_CONFIGS",
        {
            **lc._BACKEND_CONFIGS,
            Backend.OPENROUTER: replace(lc._BACKEND_CONFIGS[Backend.OPENROUTER], model=model),
        },
    )


def test_call_backend_gates_free_tier_openrouter_model_only() -> None:
    """`is_free_tier_model`/`acquire_free_tier_slot` must be consulted ONLY
    for OpenRouter `:free` models - Cave and Poolside (and a non-free
    OpenRouter model) must never pay for a limiter round-trip."""
    from adapters.pg_rate_limit_store import ReservationResult

    called: list[str] = []

    def fake_reserve(*, name, minute_limit, day_limit, now=None):
        called.append(name)
        return ReservationResult(admitted=True, minute_count=1, day_count=1)

    with patch("adapters.pg_rate_limit_store.try_reserve_slot", side_effect=fake_reserve), \
         patch.object(httpx, "post", return_value=httpx.Response(200, json={"choices": []})):
        lc._call_backend(lc._BACKEND_CONFIGS[Backend.POOLSIDE], [])
        lc._call_backend(lc._BACKEND_CONFIGS[Backend.OPENROUTER], [])  # not :free today
        assert called == []

        free_config = replace(
            lc._BACKEND_CONFIGS[Backend.OPENROUTER], model="meta-llama/x:free",
        )
        lc._call_backend(free_config, [])
        assert called == ["openrouter_free"]


def test_call_backend_raises_ratelimit_timeout_before_any_http_call(monkeypatch) -> None:
    """A limiter rejection must short-circuit BEFORE the network call - the
    call site never gets a fabricated/empty HTTP response to misread as a
    clean answer, and `RateLimitTimeoutError` is an `httpx.RequestError`
    subclass so it lands in every caller's existing transport-failure
    `except` clause with no call-site change."""
    def fake_acquire(model, *, cancel_event=None):
        from openrouter_free_limiter import RateLimitOutcome
        return RateLimitOutcome(
            admitted=False, waited_seconds=30.0, queued=True,
            minute_count=20, day_count=5, minute_limit=20, day_limit=1000,
        )

    monkeypatch.setattr(lc, "acquire_free_tier_slot", fake_acquire)
    free_config = replace(lc._BACKEND_CONFIGS[Backend.OPENROUTER], model="meta-llama/x:free")

    with patch.object(httpx, "post") as mock_post:
        with pytest.raises(lc.RateLimitTimeoutError) as exc_info:
            lc._call_backend(free_config, [])
    mock_post.assert_not_called()
    assert isinstance(exc_info.value, httpx.RequestError)


def test_openrouter_free_tier_limiter_exhaustion_never_reads_as_clean_review(monkeypatch) -> None:
    """grug#870: the load-bearing regression test. Both Cave arms down AND
    Poolside down force the SaaS-overload fallback to OpenRouter; OpenRouter
    is configured to a `:free` model whose limiter is exhausted. The result
    MUST be indistinguishable, at the vocabulary level, from any other
    total backend outage - `all_failed`, `degraded_reason` set, conclusion
    never `success` - the SAME seam PR #844/#852 built
    (`_partial_review_reason` / `_derive_conclusion`), not a new one.

    MUST fail before the `_call_backend` gate exists (OpenRouter would
    receive a normal HTTP request and, per this test's `respond()`, get a
    clean empty-findings 200 - `out.kind == "reviewed"`, conclusion
    `"success"`) and pass after it (the limiter rejects before dispatch,
    OpenRouter never gets a request, `out.kind == "all_failed"`).
    """
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    free_model = "mistralai/mistral-small:free"
    _with_openrouter_model(monkeypatch, free_model)

    def fake_acquire(model, *, cancel_event=None):
        from openrouter_free_limiter import RateLimitOutcome
        assert model == free_model
        return RateLimitOutcome(
            admitted=False, waited_seconds=30.0, queued=True,
            minute_count=20, day_count=5, minute_limit=20, day_limit=1000,
        )

    monkeypatch.setattr(lc, "acquire_free_tier_slot", fake_acquire)

    def respond(url, **kwargs):
        model = (kwargs.get("json") or {}).get("model", "")
        # Both Cave arms (same models test_saas_overload_fallback_rescues_
        # review_when_cave_fully_down fails) and Poolside - forces the SaaS
        # loop all the way to OpenRouter, the model under test here.
        if model in ("qwen3-coder-next:q8_0", "poolside/Laguna-S-2.1-NVFP4", lc._POOLSIDE_MODEL):
            raise httpx.ConnectTimeout("down")
        raise AssertionError(
            f"httpx.post must never be reached for the rate-limited model "
            f"(the limiter gate runs before dispatch) - got model={model!r}"
        )

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "all_failed", (
        f"kind={out.kind!r} - a limiter timeout must read as a backend "
        "failure, never as a reviewed/clean pass"
    )
    evaluation = evaluate_diff((), out)
    assert evaluation.degraded_reason is not None
    assert evaluation.conclusion != "success", (
        f"conclusion={evaluation.conclusion!r} - exhausting the OpenRouter "
        'free-tier queue must never surface as "Elder clear - no markings"'
    )


def test_parse_failed_attributes_secondary_backend(monkeypatch) -> None:
    """If the primary backend transport-fails and the secondary returns
    200 + non-JSON content, parse_failed must report the secondary as
    `backend_used`. (The secondary is the only backend that produced a 200,
    so there's nothing further to fall back to.) Verify the attribution
    points at whoever actually responded."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    parse_fail_envelope = _openai_json_response("sorry, I cannot do that")

    def staged(url, *args, **kwargs):
        if "Laguna-S-2.1" not in (kwargs.get("json") or {}).get("model", ""):
            raise httpx.ReadTimeout("coder arm down")
        return httpx.Response(200, json=parse_fail_envelope)

    with patch.object(httpx, "post", side_effect=staged):
        out = review_diff([_hunk()], installation_id=2)

    assert out.kind == "parse_failed"
    assert out.backend_used == Backend.CAVE_REASONER
    assert "parse" in out.error.lower()


def test_parse_failure_on_primary_falls_back_to_secondary(monkeypatch) -> None:
    """A 200-but-unparseable response from the PRIMARY must fall back to the
    secondary — the two backends run different models (claude vs laguna), so a
    parse failure on one doesn't predict the other. Primary parse-fails,
    secondary returns clean JSON → kind=reviewed, attributed to the secondary.
    """
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    good = _openai_json_response(
        '{"findings":[{"path":"x.py","line":1,"rule":"ok",'
        '"severity":"low","message":"m"}]}'
    )
    bad = _openai_json_response("sorry, no JSON here")

    def staged(url, *args, **kwargs):
        # Coder arm (primary) parse-fails; reasoner arm (secondary) returns clean.
        if "Laguna-S-2.1" not in (kwargs.get("json") or {}).get("model", ""):
            return httpx.Response(200, json=bad)
        return httpx.Response(200, json=good)

    with patch.object(httpx, "post", side_effect=staged):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.CAVE_REASONER
    assert len(out.findings) == 1


def test_both_parse_fail_returns_parse_failed_attributed_to_primary(monkeypatch) -> None:
    """When BOTH backends return 200-but-unparseable, fall back is exhausted;
    surface the specific parse_failed kind (not all_failed), attributed to the
    PRIMARY (the first parse failure)."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    bad = httpx.Response(200, json=_openai_json_response("nope, prose only"))
    with patch.object(httpx, "post", return_value=bad):
        out = review_diff([_hunk()], installation_id=1)
    assert out.kind == "parse_failed"
    assert out.backend_used == Backend.CAVE  # first (coder) arm


def test_non_dict_finding_entries_dropped() -> None:
    """Under JSON-mode pressure, LLMs sometimes emit a string or scalar
    where the schema asks for a dict. Drop the entry rather than crashing
    on attribute access downstream."""
    findings_json = (
        '{"findings": ['
        '"just a string",'
        'null,'
        '42,'
        '{"path": "y", "line": 5, "rule": "ok", "severity": "low", "message": ""}'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert len(out.findings) == 1
    assert out.findings[0].rule == "ok"


def test_bad_type_finding_entry_dropped() -> None:
    """`line` is non-coercible (a list, not int-castable). `_coerce_finding`
    must catch TypeError/ValueError and drop, not crash."""
    findings_json = (
        '{"findings": ['
        '{"path": "x", "line": [1, 2], "rule": "bad", "severity": "high", "message": ""},'
        '{"path": "y", "line": 5, "rule": "ok", "severity": "low", "message": ""}'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert len(out.findings) == 1
    assert out.findings[0].rule == "ok"


def test_envelope_json_array_returns_parse_failed() -> None:
    """200 with a JSON array (not a dict) — provider edge case where the
    response shape is wrong. Must not AttributeError on `body['choices']`."""
    response = httpx.Response(200, json=["not", "a", "dict"])

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "parse_failed"
    assert "envelope" in out.error.lower() or "dict" in out.error.lower()


def test_envelope_missing_choices_returns_parse_failed() -> None:
    """Both providers return `{"error": {"code": "..."}}` on bad payloads —
    a valid JSON dict without `choices`. Must surface as parse_failed,
    not raise."""
    response = httpx.Response(
        200, json={"error": {"code": "invalid_request", "message": "bad"}}
    )

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "parse_failed"
    assert "choices" in out.error.lower() or "missing" in out.error.lower()


def test_non_retryable_5xx_does_not_burn_retry_budget(monkeypatch) -> None:
    """500/502/504 exit the retry loop immediately. A regression that
    adds them to `_RETRYABLE_STATUSES` would 3x latency before fallback —
    catch it by asserting only 1 attempt per backend."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    call_log: list[str] = []

    def staged(url, *args, **kwargs):
        call_log.append(url)
        return httpx.Response(502, text="bad gateway")

    with patch.object(httpx, "post", side_effect=staged):
        out = review_diff([_hunk()], installation_id=2)

    assert out.kind == "all_failed"
    # 1 attempt per backend x 4 backends (both Cave arms, then the
    # OpenRouter/Poolside overload fallback since Cave produced nothing
    # usable) = 4 calls. Not 12 (would be retried).
    assert len(call_log) == 4


def test_envelope_non_json_returns_parse_failed(monkeypatch) -> None:
    """200 + Cloudflare HTML interstitial (not JSON) must not crash.
    Previously `_parse_response` called `resp.json()` unguarded — the
    JSONDecodeError would bubble through `review_diff` and 500 the
    webhook handler. Now it returns a parse_failed envelope so the
    caller can post an advisory check-run."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    response = httpx.Response(200, text="<html>error</html>")
    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)
    # 200+non-JSON short-circuits to parse_failed (no fallback — the
    # other backend would likely return the same edge HTML).
    assert out.kind == "parse_failed"
    assert "envelope" in out.error.lower() or "json" in out.error.lower()


def test_parse_response_handles_list_shaped_content() -> None:
    """#416: a model returning a bare JSON ARRAY of findings (not the
    documented {"findings": [...]} object) must be parsed as the findings
    list, NOT crash with `'list' object has no attribute 'get'` (which dropped
    a live Elder review, delivery 831476f0)."""
    content = '[{"rule": "x", "path": "p", "line": 1, "severity": "low"}]'
    findings, _model, err = lc._parse_response(
        httpx.Response(200, json=_openai_json_response(content))
    )
    assert err == ""
    assert len(findings) == 1


def test_parse_response_scalar_content_is_graceful_not_crash() -> None:
    """#416: content that is valid JSON but neither dict nor list (a bare
    string/number) returns a graceful parse-failure error, never an unhandled
    exception."""
    findings, _model, err = lc._parse_response(
        httpx.Response(200, json=_openai_json_response('"just a string"'))
    )
    assert findings == ()
    assert err  # non-empty error string, no exception raised


def test_parse_response_null_content_with_reasoning_is_clean_parse_error() -> None:
    """grug#881: a THINKING model under `response_format=json_object` can
    legally return `content: null` while its answer landed in a separate
    `reasoning` field instead of raising. `json.loads(None)` raises
    `TypeError`, not `JSONDecodeError` - `_parse_response`'s whole job is
    safe parsing, so this must come back as a normal (findings, model,
    error) triple, never an unhandled exception. The error must name the
    offending field (content is null) AND finish_reason, so one run is
    enough to diagnose - the whole point of grug#881."""
    body = {
        "model": "poolside/laguna-s-2.1:free",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning": "Here's a thinking process:\n\n1. **An",
                },
                "finish_reason": "length",
            }
        ],
    }
    findings, model, err = lc._parse_response(httpx.Response(200, json=body))
    assert findings == ()
    assert model == "poolside/laguna-s-2.1:free"
    assert err  # non-empty, no exception raised
    assert "null" in err
    assert "finish_reason=length" in err
    assert "reasoning field present" in err


def test_parse_response_null_content_without_reasoning_still_diagnoses() -> None:
    """grug#881: the ACTUAL production request shape
    (`reasoning: {"exclude": True}`) verified live against
    `poolside/laguna-s-2.1:free` - `reasoning` comes back None too, not
    just `content`. Excluded means excluded; there is nothing to fall back
    to. The error must still name the offending field and finish_reason
    with no `reasoning` field present at all."""
    body = {
        "model": "poolside/laguna-s-2.1:free",
        "choices": [
            {
                "message": {"role": "assistant", "content": None},
                "finish_reason": "length",
            }
        ],
    }
    findings, _model, err = lc._parse_response(httpx.Response(200, json=body))
    assert findings == ()
    assert err
    assert "null" in err
    assert "finish_reason=length" in err
    assert "reasoning field present" not in err


def test_parse_response_non_string_content_diagnoses_type() -> None:
    """A content value that is present but the WRONG type (e.g. a dict, if
    a provider ever nests structured output there) must also return a
    diagnosable error naming the actual type, never raise."""
    body = {
        "model": "some-model",
        "choices": [
            {
                "message": {"role": "assistant", "content": {"nested": "oops"}},
                "finish_reason": "stop",
            }
        ],
    }
    findings, _model, err = lc._parse_response(httpx.Response(200, json=body))
    assert findings == ()
    assert err
    assert "dict" in err
    assert "finish_reason=stop" in err


def test_parse_response_empty_string_content_names_finish_reason_and_reasoning() -> None:
    """grug#881: the shape the issue observed LIVE on the in-cluster gateway
    was `content: ""` (an empty STRING, not null) with the whole token
    budget spent in `reasoning` and `finish_reason: "length"`. That is a
    str, so it skipped the non-string diagnosis and came back as the
    generic non-json error - `len=0` and nothing else. An operator reading
    that line cannot tell "the model spent its budget thinking" (length)
    from "the model answered nothing" (stop). Empty content must get the
    same one-line diagnosis as null: the offending field, finish_reason,
    and whether a reasoning field was there."""
    body = {
        "model": "some-thinking-model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "Here's a thinking process:\n\n1. **An",
                },
                "finish_reason": "length",
            }
        ],
    }
    findings, model, err = lc._parse_response(httpx.Response(200, json=body))
    assert findings == ()
    assert model == "some-thinking-model"
    assert err
    assert "content is empty str" in err
    assert "finish_reason=length" in err
    assert "reasoning field present" in err


def test_parse_response_whitespace_only_content_is_diagnosed_as_empty() -> None:
    """Whitespace-only content is the same failure as `""` - nothing to
    parse - and must not slip back into the generic non-json path just
    because len() is non-zero."""
    body = {
        "model": "m",
        "choices": [
            {"message": {"role": "assistant", "content": "\n  \n"}, "finish_reason": "stop"}
        ],
    }
    findings, _model, err = lc._parse_response(httpx.Response(200, json=body))
    assert findings == ()
    assert "content is empty str" in err
    assert "finish_reason=stop" in err
    assert "reasoning field present" not in err


# ---------------------------------------------------------------------------
# DD LLM Obs tracing — every successful LLM call emits a trace span with
# prompt/response/latency/tokens; failures emit a span with error metadata.
# ---------------------------------------------------------------------------

def _capture_llmobs(monkeypatch):
    """Patch LLMObs.llm + LLMObs.annotate; return a list of all
    annotate calls so tests can introspect the trace shape."""
    annotate_calls: list[dict] = []

    class _FakeSpan:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(lc, "_llmobs_llm", lambda **kw: _FakeSpan())
    monkeypatch.setattr(
        lc, "_llmobs_annotate",
        lambda **kw: annotate_calls.append(kw),
    )
    return annotate_calls


def test_review_diff_emits_llmobs_span_on_success(monkeypatch) -> None:
    """Every successful LLM call must emit a DD LLM Obs span carrying
    prompt + response + latency_ms + tokens + model + backend."""
    annotate_calls = _capture_llmobs(monkeypatch)
    body = _openai_json_response('{"findings":[]}')
    body["usage"] = {"prompt_tokens": 100, "completion_tokens": 25}
    response = httpx.Response(200, json=body)
    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert len(annotate_calls) == 1
    call = annotate_calls[0]
    # Prompt = the messages array (system + user).
    assert isinstance(call["input_data"], list)
    assert call["input_data"][0]["role"] == "system"
    # Response = the model's content string.
    assert call["output_data"] == '{"findings":[]}'
    # Metrics include tokens + latency.
    metrics = call["metrics"]
    assert metrics["input_tokens"] == 100
    assert metrics["output_tokens"] == 25
    assert "latency_ms" in metrics
    assert metrics["latency_ms"] >= 0
    # Metadata names the backend (fast mode returns after the coder arm).
    assert call["metadata"]["backend"] == "cave"
    # #191: and the prompt experiment arm, so DD can slice eval results by it.
    assert call["metadata"]["variant_id"] == "v1"  # default mode off → v1


def test_review_diff_llmobs_span_variant_id_reflects_experiment(monkeypatch) -> None:
    """When the experiment forces v2, the span metadata must carry it on the
    success path — the variant_id is what makes the A/B analyzable in DD."""
    monkeypatch.setattr(lc, "get_prompt_experiment_mode", lambda: "all_v2")
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    with patch.object(httpx, "post", return_value=response):
        review_diff([_hunk()], installation_id=1)
    assert annotate_calls[0]["metadata"]["variant_id"] == "v2"


def test_review_diff_llmobs_span_carries_pr_context_tags(monkeypatch) -> None:
    """The PR coords (install_id, repo, pr_number, head_sha) must flow
    into span tags so DD LLM Obs can filter traces by repo or PR."""
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    pr_context = {
        "installation_id": 42,
        "repo": "myorg/myrepo",
        "pr_number": 7,
        "head_sha": "abc123def456",
    }
    with patch.object(httpx, "post", return_value=response):
        review_diff(
            [_hunk()], installation_id=42, pr_context=pr_context,
        )

    tags = annotate_calls[0]["tags"]
    assert tags["installation_id"] == "42"
    assert tags["repo"] == "myorg/myrepo"
    assert tags["pr_number"] == "7"
    # head_sha truncated to 8 chars to keep tag cardinality bounded.
    assert tags["head_sha"] == "abc123de"


def test_review_diff_emits_llmobs_span_on_transport_failure(monkeypatch) -> None:
    """A backend timeout must STILL emit an LLM Obs span so the failure
    is visible in DD (latency tail, error rate). Span metadata names
    the error class; output_data is absent."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    annotate_calls = _capture_llmobs(monkeypatch)

    def _timeout(*a, **kw):
        raise httpx.ReadTimeout("hung")

    with patch.object(httpx, "post", side_effect=_timeout):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "all_failed"
    # Both Cave arms tried, then the OpenRouter/Poolside overload fallback
    # (Cave produced nothing usable) → four spans.
    assert len(annotate_calls) == 4
    for call in annotate_calls:
        # Error class captured in metadata.
        assert call["metadata"].get("error") == "ReadTimeout"
        # No output content on a transport failure.
        assert call.get("output_data") is None
        # #191: the A/B arm must ride the ERROR span too, or failure-rate-by-arm
        # is unattributable in DD (default mode off → v1).
        assert call["metadata"]["variant_id"] == "v1"


def test_review_diff_llmobs_span_handles_missing_usage(monkeypatch) -> None:
    """OpenRouter free-tier sometimes omits the `usage` field. Span
    must not crash - unavailable token metrics are omitted."""
    annotate_calls = _capture_llmobs(monkeypatch)
    # OpenAI shape but NO usage key.
    body = {"choices": [{"message": {"content": '{"findings":[]}'}}], "model": "x"}
    response = httpx.Response(200, json=body)
    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)
    assert out.kind == "reviewed"
    metrics = annotate_calls[0]["metrics"]
    # latency must still be present even when tokens are missing.
    assert "latency_ms" in metrics
    assert "input_tokens" not in metrics
    assert "output_tokens" not in metrics


def test_llmobs_span_annotate_called_exactly_once_per_backend_attempt(monkeypatch) -> None:
    """Per backend attempt, the `with _llmobs_llm(...)` block must call
    `_llmobs_annotate` exactly ONCE — no double-annotation across the
    success/config-error/transport-error branches. Future refactors
    adding an early `continue` could double-annotate; lock the count."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    annotate_calls = _capture_llmobs(monkeypatch)
    # Primary (coder) arm times out (no retry - review transport errors get
    # exactly one attempt), secondary (reasoner) arm succeeds on its own
    # first attempt - Cave produces a usable response, so the OpenRouter/
    # Poolside overload fallback never engages (kept out of this test
    # deliberately; it has its own dedicated span-count coverage).
    seq: list = [
        httpx.ReadTimeout("p1"),
        httpx.Response(200, json=_openai_json_response('{"findings":[]}')),
    ]
    idx = {"n": 0}

    def staged(*a, **kw):
        i = idx["n"]
        idx["n"] += 1
        x = seq[i]
        if isinstance(x, Exception):
            raise x
        return x

    with patch.object(httpx, "post", side_effect=staged):
        review_diff([_hunk()], installation_id=1)

    # Exactly 2 spans (one per backend attempt — primary timeout +
    # secondary success). Not 3 (one per httpx.post retry) — the span
    # wraps the whole `_call_backend`, not each retry.
    assert len(annotate_calls) == 2


def test_llmobs_tags_match_pr_context_keys(monkeypatch) -> None:
    """Lock the tag-key set so a future `PrContext` field addition is
    a deliberate edit to _llmobs_tags, not a silent drop. If PrContext
    grows a `branch` key but _llmobs_tags doesn't, this test fails."""
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    with patch.object(httpx, "post", return_value=response):
        review_diff(
            [_hunk()], installation_id=42,
            pr_context={
                "installation_id": 42, "repo": "o/r", "pr_number": 1,
                "head_sha": "abc123def456",
            },
        )
    assert set(annotate_calls[0]["tags"].keys()) == {
        "installation_id", "repo", "pr_number", "head_sha",
    }


def test_llmobs_config_error_annotates_with_error_config(monkeypatch) -> None:
    """_BackendConfigError path must annotate with metadata.error=`config`.
    Without this signal DD dashboards see only transport errors and
    can't tell `secret missing` from `backend down`."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    # No gateway URL → both ensemble arms raise _BackendConfigError before any HTTP.
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL", raising=False)
    # Force the v2 arm so we assert the experiment arm rides the config-error
    # span too (not just success) — #191 failure-rate-by-arm depends on it.
    monkeypatch.setattr(lc, "get_prompt_experiment_mode", lambda: "all_v2")
    annotate_calls = _capture_llmobs(monkeypatch)

    # Cave arms fail the config guard before any HTTP call; the OpenRouter/
    # Poolside overload fallback that follows (Cave produced nothing usable)
    # IS configured (autouse _patch_keys), so it does make HTTP calls - give
    # it a clean transport failure so its spans are distinguishable
    # (error=ConnectError, not error=config) from the Cave arms' spans.
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("down")) as mock_post:
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "all_failed"
    # Cave never dialed HTTP; only the two overload-fallback attempts did.
    assert mock_post.call_count == 2
    # Two Cave arms each fail config check (error=config), then two SaaS
    # overload-fallback attempts each transport-fail (error=ConnectError).
    assert len(annotate_calls) == 4
    cave_calls, saas_calls = annotate_calls[:2], annotate_calls[2:]
    for call in cave_calls:
        assert call["metadata"].get("error") == "config"
        # output_data absent on config error.
        assert call.get("output_data") is None
        # #191: arm attribution present on the config-error path.
        assert call["metadata"]["variant_id"] == "v2"
    for call in saas_calls:
        assert call["metadata"].get("error") == "ConnectError"
        assert call.get("output_data") is None


def test_llmobs_metadata_kind_parse_failed_on_200_with_bad_content(monkeypatch) -> None:
    """When the LLM returns 200 + non-JSON content, the span metadata
    must tag kind="parse_failed" (not "reviewed", not "http_error").
    Locks the ternary order on the success-annotate path."""
    annotate_calls = _capture_llmobs(monkeypatch)
    # 200 envelope is valid JSON, but the message.content is not JSON.
    response = httpx.Response(200, json=_openai_json_response("sorry I cannot"))
    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)
    assert out.kind == "parse_failed"
    assert annotate_calls[0]["metadata"]["kind"] == "parse_failed"


def test_llmobs_metadata_kind_http_error_on_non_200(monkeypatch) -> None:
    """Non-200 status → metadata.kind="http_error" (not "parse_failed"
    or "reviewed"). DD dashboards aggregate by this facet — a
    mislabel would undercount backend health rate."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    annotate_calls = _capture_llmobs(monkeypatch)
    # 500 with no retryable status; both backends will return 500.
    response = httpx.Response(500, json={"error": "down"})
    with patch.object(httpx, "post", return_value=response):
        review_diff([_hunk()], installation_id=1)
    # Both backends tagged http_error.
    for call in annotate_calls:
        assert call["metadata"]["kind"] == "http_error"
        assert call["metadata"]["status_code"] == 500


def test_extract_usage_metrics_handles_non_dict_usage() -> None:
    """A future backend that returns `usage` as a string or list must
    not crash. The `isinstance(usage, dict)` guard is the load-bearing
    one — removing it would AttributeError on `.get()`."""
    # body with usage=list (degenerate).
    out = lc._extract_usage_metrics({"usage": [1, 2, 3]})
    assert out == {}
    # body that itself isn't a dict.
    out = lc._extract_usage_metrics("not a dict")
    assert out == {}
    # body=None (defensive — the upstream re-parse fallback sets body={}
    # but a future caller might pass None).
    out = lc._extract_usage_metrics(None)
    assert out == {}


def test_extract_usage_metrics_keeps_only_finite_numeric_values() -> None:
    assert lc._extract_usage_metrics({
        "usage": {"prompt_tokens": 12, "completion_tokens": 4.5},
    }) == {"input_tokens": 12, "output_tokens": 4.5}
    assert lc._extract_usage_metrics({
        "usage": {"prompt_tokens": "12", "completion_tokens": None},
    }) == {}
    assert lc._extract_usage_metrics({
        "usage": {"prompt_tokens": True, "completion_tokens": float("nan")},
    }) == {}
    assert lc._extract_usage_metrics({
        "usage": {"prompt_tokens": 12, "completion_tokens": None},
    }) == {"input_tokens": 12}
    assert lc._extract_usage_metrics({
        "usage": {"prompt_tokens": float("inf"), "completion_tokens": 4},
    }) == {"output_tokens": 4}
    assert lc._extract_usage_metrics({
        "usage": {"prompt_tokens": float("-inf"), "completion_tokens": -1},
    }) == {}


def test_llmobs_body_reparse_failure_logs_warning(monkeypatch, caplog) -> None:
    """The re-parse except branch logs `llm_body_reparse_failed`. A
    regression that drops the log line (or that swaps `except` to
    `Exception` and masks an unrelated bug) silently misses the DD
    alert. Pin the log emission to a discriminator the test can read."""
    annotate_calls = _capture_llmobs(monkeypatch)

    # Build a Response where the first .json() succeeds (during
    # _parse_response — invalid `choices` shape returns err=missing
    # choices) AND the second .json() (the re-parse) also returns 200.
    # We force a divergence by stubbing _parse_response to return
    # success (err="") but stubbing .json() the second time to raise.
    call_count = {"n": 0}

    def fake_json(self):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call from _parse_response — return a valid envelope.
            return _openai_json_response('{"findings":[]}')
        raise ValueError("divergent")

    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    monkeypatch.setattr(httpx.Response, "json", fake_json)
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            review_diff([_hunk()], installation_id=1)
    assert any(
        "llm_body_reparse_failed" in r.message for r in caplog.records
    )
    # Span still emitted, just with empty output.
    assert annotate_calls[0]["metadata"]["backend"] == "cave"


def test_redact_secrets_strips_aws_github_pem_env_patterns() -> None:
    """Defense-in-depth atop the DD org-level sensitive data scanner.
    We must not ship raw secrets across the wire in the first place —
    a PR diff that touches a .env or accidentally commits a key file
    should not persist in DD storage as plaintext."""
    raw = (
        "AKIAIOSFODNN7EXAMPLE in some code "
        "and ghp_1234567890abcdefghijklmnopqrstuvwxyzAB github token "
        "and PASSWORD=supersecretvalue12345 env line "
        "and -----BEGIN RSA PRIVATE KEY-----\nMIIEpAIB\n-----END RSA PRIVATE KEY----- pem"
    )
    out = lc._redact_secrets(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[REDACTED:aws-access-key]" in out
    assert "ghp_1234567890" not in out
    assert "[REDACTED:github-pat]" in out
    assert "supersecretvalue" not in out
    assert "[REDACTED:env-secret]" in out
    assert "MIIEpAIB" not in out
    assert "[REDACTED:pem-private-key]" in out


def test_redact_payload_walks_message_list_structure() -> None:
    """The OpenAI-compat `messages` payload is `list[dict[str, str]]`.
    Redaction must walk the structure — not just stringify it — so
    nested string values get scrubbed without losing the shape."""
    messages = [
        {"role": "system", "content": "You are a reviewer"},
        {"role": "user", "content": "diff: PASSWORD=secret12345 line"},
    ]
    out = lc._redact_payload(messages)
    assert isinstance(out, list)
    assert out[0]["role"] == "system"
    assert "secret12345" not in out[1]["content"]
    assert "[REDACTED:env-secret]" in out[1]["content"]


def test_redact_payload_truncates_after_redaction() -> None:
    """Truncation runs AFTER redaction so a trailing PEM fragment can't
    survive a mid-string cut. Bound exposure even when no patterns
    match (massive lockfile diff)."""
    huge = "x" * 100_000
    out = lc._redact_payload(huge)
    assert len(out) == lc._LLMOBS_PAYLOAD_TRUNC_BYTES


def test_llmobs_input_data_is_redacted_on_success(monkeypatch) -> None:
    """End-to-end: a diff containing a fake AWS key reaches the span
    redacted. Catches a regression where _redact_payload is dropped
    from the input_data argument."""
    annotate_calls = _capture_llmobs(monkeypatch)
    leaky_hunk = lc.Hunk(
        path="bad.py",
        body="+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'  # oops",
    )
    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    with patch.object(httpx, "post", return_value=response):
        review_diff([leaky_hunk], installation_id=1)
    span_input_str = json.dumps(annotate_calls[0]["input_data"])
    assert "AKIAIOSFODNN7EXAMPLE" not in span_input_str
    assert "[REDACTED:aws-access-key]" in span_input_str


def test_llmobs_output_data_is_redacted_on_success(monkeypatch) -> None:
    """A hallucinating LLM might echo a secret back in its response.
    output_data must also pass through _redact_payload."""
    annotate_calls = _capture_llmobs(monkeypatch)
    leaky_content = (
        '{"findings": [{"path": "x", "line": 1, "rule": "leak", '
        '"severity": "critical", "message": "found AKIAIOSFODNN7EXAMPLE"}]}'
    )
    response = httpx.Response(200, json=_openai_json_response(leaky_content))
    with patch.object(httpx, "post", return_value=response):
        review_diff([_hunk()], installation_id=1)
    out_str = str(annotate_calls[0]["output_data"])
    assert "AKIAIOSFODNN7EXAMPLE" not in out_str
    assert "[REDACTED:aws-access-key]" in out_str


def test_build_messages_redacts_secrets_in_diff(monkeypatch) -> None:
    """#438: secrets in the diff are masked in the user message BEFORE it is sent
    to the backend (a third-party SaaS endpoint), not just in the DD span."""
    hunks = [lc.Hunk(path="bad.py", body="+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'")]
    user = lc._build_messages(hunks, "v1")[1]["content"]
    assert "AKIAIOSFODNN7EXAMPLE" not in user
    assert "[REDACTED:aws-access-key]" in user


def test_build_messages_redacts_secrets_in_file_context() -> None:
    """#438: the full-file context block (#336) is also redacted - a secret on an
    UNCHANGED line of a changed file must not reach the backend either."""
    hunks = [lc.Hunk(path="bad.py", body="+x = 1")]
    user = lc._build_messages(hunks, "v1", {"bad.py": "KEY = 'AKIAIOSFODNN7EXAMPLE'\nx = 1\n"})[1]["content"]
    assert "AKIAIOSFODNN7EXAMPLE" not in user


def test_backend_request_body_is_redacted(monkeypatch) -> None:
    """#438 end-to-end: the body sent to _call_backend / httpx.post has secrets
    redacted. This is THE acceptance criterion - the SaaS backend never receives
    a raw secret from the main review."""
    leaky_hunk = lc.Hunk(path="bad.py", body="+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'  # oops")
    captured: dict = {}

    def _capture_post(url, **kw):
        captured["json"] = kw.get("json")
        return httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    monkeypatch.setattr(httpx, "post", _capture_post)
    review_diff([leaky_hunk], installation_id=1)
    body_str = json.dumps(captured["json"])
    assert "AKIAIOSFODNN7EXAMPLE" not in body_str
    assert "[REDACTED:aws-access-key]" in body_str


def test_no_diff_short_circuit_does_not_emit_llmobs_span(monkeypatch) -> None:
    """Empty hunks short-circuit before any LLM call — no span should
    be emitted (no LLM call happened)."""
    annotate_calls = _capture_llmobs(monkeypatch)
    out = review_diff([], installation_id=1)
    assert out.kind == "no_diff"
    assert annotate_calls == []


def test_review_diff_carries_exported_span_context_on_success(monkeypatch) -> None:
    """The review span is exported onto the response so the LLM-as-a-
    judge (slice #190) can attach per-finding `is_real_bug` evaluations
    to the exact span whose output produced the findings."""
    _capture_llmobs(monkeypatch)
    monkeypatch.setattr(
        lc, "_llmobs_export", lambda span: {"span_id": "s1", "trace_id": "t1"},
    )
    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)
    assert out.kind == "reviewed"
    assert out.review_span_context == {"span_id": "s1", "trace_id": "t1"}


# ---------------------------------------------------------------------------
# LLM-as-a-judge (#190) — second LLM call scores each finding is_real_bug.
# ---------------------------------------------------------------------------

def test_judge_findings_makes_second_llm_call_and_parses_verdicts(monkeypatch) -> None:
    """judge_findings sends the findings + diff to a second LLM call and
    parses a verdicts array into FindingJudgement objects."""
    _capture_llmobs(monkeypatch)
    verdicts_json = (
        '{"verdicts": ['
        '{"index": 0, "is_real_bug": true, "reasoning": "real null deref"},'
        '{"index": 1, "is_real_bug": false, "reasoning": "style nit, not a bug"}'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(verdicts_json))
    findings_repr = [
        {"rule_name": "null-deref", "file": "x.py", "line": 2, "message": "m1"},
        {"rule_name": "style", "file": "x.py", "line": 3, "message": "m2"},
    ]
    with patch.object(httpx, "post", return_value=response):
        out = lc.judge_findings(findings_repr, [_hunk()], installation_id=1)

    assert len(out) == 2
    assert out[0].finding_index == 0
    assert out[0].is_real_bug is True
    assert out[1].finding_index == 1
    assert out[1].is_real_bug is False
    assert "style nit" in out[1].reasoning


def test_judge_findings_emits_its_own_llmobs_span(monkeypatch) -> None:
    """The judge LLM call is itself traced (own span, name elder_judge)
    so its prompt/latency/tokens/cost show up in DD distinct from the
    review call."""
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"verdicts":[]}'))
    findings_repr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with patch.object(httpx, "post", return_value=response):
        lc.judge_findings(findings_repr, [_hunk()], installation_id=1)
    # judge call emits a span tagged judge=True (distinct from the
    # review span) even when the LLM returns zero verdicts.
    assert len(annotate_calls) == 1
    assert annotate_calls[0]["metadata"]["judge"] is True


def test_judge_findings_returns_empty_on_llm_failure(monkeypatch) -> None:
    """If the judge LLM call fails (transport / parse), return empty —
    the judge is best-effort observability, never blocks the review."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    _capture_llmobs(monkeypatch)

    def _timeout(*a, **kw):
        raise httpx.ReadTimeout("judge backend down")

    findings_repr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with patch.object(httpx, "post", side_effect=_timeout):
        out = lc.judge_findings(findings_repr, [_hunk()], installation_id=1)
    assert out == ()


def test_judge_findings_skips_above_max_findings(monkeypatch, caplog) -> None:
    """Cost guard: a firehose review (> _JUDGE_MAX_FINDINGS) skips the
    judge LLM call entirely — no second-call token spend, logged so the
    skip is visible."""
    _capture_llmobs(monkeypatch)
    too_many = [
        {"rule_name": f"r{i}", "file": "x.py", "line": i + 1, "message": "m"}
        for i in range(lc._JUDGE_MAX_FINDINGS + 1)
    ]
    with caplog.at_level("INFO"):
        with patch.object(httpx, "post") as mock_post:
            out = lc.judge_findings(too_many, [_hunk()], installation_id=1)
    assert out == ()
    mock_post.assert_not_called()
    assert any(
        "judge_skipped_too_many_findings" in r.message for r in caplog.records
    )


def test_judge_findings_at_max_findings_still_runs(monkeypatch) -> None:
    """Exactly _JUDGE_MAX_FINDINGS findings is within budget — the
    judge still runs (boundary is `>`, not `>=`)."""
    _capture_llmobs(monkeypatch)
    at_limit = [
        {"rule_name": f"r{i}", "file": "x.py", "line": i + 1, "message": "m"}
        for i in range(lc._JUDGE_MAX_FINDINGS)
    ]
    response = httpx.Response(200, json=_openai_json_response('{"verdicts":[]}'))
    with patch.object(httpx, "post", return_value=response) as mock_post:
        lc.judge_findings(at_limit, [_hunk()], installation_id=1)
    mock_post.assert_called()


def test_judge_findings_empty_findings_short_circuits(monkeypatch) -> None:
    """No findings → no judge call (nothing to evaluate)."""
    annotate_calls = _capture_llmobs(monkeypatch)
    with patch.object(httpx, "post") as mock_post:
        out = lc.judge_findings([], [_hunk()], installation_id=1)
    # Empty findings is a legit "nothing to judge" — skip the LLM call.
    assert out == ()
    mock_post.assert_not_called()
    assert annotate_calls == []


def test_judge_findings_redacts_secrets_in_judge_span(monkeypatch) -> None:
    """The judge prompt embeds the diff too — must redact before the
    span leaves the process, same as the review span."""
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"verdicts":[]}'))
    leaky_hunk = lc.Hunk(path="x.py", body="+key='AKIAIOSFODNN7EXAMPLE'")
    findings_repr = [{"rule_name": "r", "file": "x.py", "line": 1, "message": "m"}]
    with patch.object(httpx, "post", return_value=response):
        lc.judge_findings(findings_repr, [leaky_hunk], installation_id=1)
    span_input = json.dumps(annotate_calls[0]["input_data"])
    assert "AKIAIOSFODNN7EXAMPLE" not in span_input
    assert "[REDACTED:aws-access-key]" in span_input


def test_submit_finding_evaluation_calls_dd_seam(monkeypatch) -> None:
    """submit_finding_evaluation maps is_real_bug → a DD LLM Obs
    categorical evaluation attached to the review span."""
    eval_calls: list[dict] = []
    monkeypatch.setattr(
        lc, "_llmobs_submit_evaluation", lambda **kw: eval_calls.append(kw),
    )
    span_ctx = {"span_id": "s1", "trace_id": "t1"}
    lc.submit_finding_evaluation(
        is_real_bug=True,
        reasoning="real bug",
        review_span_context=span_ctx,
        tags={"rule_name": "null-deref", "file": "x.py", "line": "2"},
    )
    assert len(eval_calls) == 1
    call = eval_calls[0]
    assert call["label"] == "is_real_bug"
    assert call["metric_type"] == "categorical"
    assert call["value"] == "true"
    assert call["span"] == span_ctx
    assert call["tags"]["rule_name"] == "null-deref"
    # reasoning surfaced for the annotation-queue reviewer.
    assert call["reasoning"] == "real bug"


def test_submit_finding_evaluation_false_maps_to_string_false(monkeypatch) -> None:
    eval_calls: list[dict] = []
    monkeypatch.setattr(
        lc, "_llmobs_submit_evaluation", lambda **kw: eval_calls.append(kw),
    )
    lc.submit_finding_evaluation(
        is_real_bug=False, reasoning="fp",
        review_span_context={"span_id": "s"}, tags={},
    )
    assert eval_calls[0]["value"] == "false"


def test_judge_unparseable_response_logs_warning(monkeypatch, caplog) -> None:
    """A judge whose every response is non-JSON must be distinguishable
    in logs from a judge that legitimately returned zero verdicts —
    else the ground-truth dataset stops growing invisibly."""
    _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response("not json prose"))
    findings_repr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            out = lc.judge_findings(findings_repr, [_hunk()], installation_id=1)
    assert out == ()
    assert any("judge_verdicts_unparseable" in r.message for r in caplog.records)


def test_judge_unparseable_log_redacts_secrets(monkeypatch, caplog) -> None:
    """The drop-path log captures raw judge content — which (the judge
    saw the diff) may echo a secret. It MUST route through
    `_redact_secrets` before landing in DD logs, same as the span."""
    _capture_llmobs(monkeypatch)
    # 200 + non-JSON content that contains a fake AWS key.
    leaky = "prose not json AKIAIOSFODNN7EXAMPLE trailing"
    response = httpx.Response(200, json=_openai_json_response(leaky))
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            lc.judge_findings(fr, [_hunk()], installation_id=1)
    rec = next(r for r in caplog.records if r.message == "judge_verdicts_unparseable")
    assert "AKIAIOSFODNN7EXAMPLE" not in rec.__dict__["raw"]
    assert "[REDACTED:aws-access-key]" in rec.__dict__["raw"]


def test_judge_partial_drop_logs_count(monkeypatch, caplog) -> None:
    """Some verdicts valid, some malformed → logged drop count so a
    creeping malformation rate is visible."""
    _capture_llmobs(monkeypatch)
    verdicts = (
        '{"verdicts": ['
        '{"index": 0, "is_real_bug": true, "reasoning": "ok"},'
        '{"garbage": "no index"},'
        '"a string not a dict"'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(verdicts))
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            out = lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert len(out) == 1
    rec = next(r for r in caplog.records if r.message == "judge_verdicts_partial_drop")
    assert rec.__dict__["dropped"] == 2
    assert rec.__dict__["kept"] == 1


@pytest.mark.parametrize("raw", ["false", "true", 0, 1, None, []])
def test_judge_rejects_non_boolean_is_real_bug(raw) -> None:
    """JSON strings are truthy in Python: bool("false") is True. The judge
    boundary must accept actual JSON booleans only or it can invert a verdict."""
    verdicts = lc._parse_judge_verdicts(json.dumps({
        "verdicts": [{
            "index": 0,
            "is_real_bug": raw,
            "confidence": 0.9,
            "reasoning": "test",
        }]
    }))
    assert verdicts == ()


def test_judge_non_200_returns_empty_no_empty_content_warning(monkeypatch, caplog) -> None:
    """A non-200 judge response (rate-limited / 5xx) → body={}, no
    content, returns (). The `judge_empty_content` warning is gated on
    status==200 so it must NOT fire here (that warning means '200 but
    garbage', a different failure)."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    _capture_llmobs(monkeypatch)
    # 500 on both retries — _call_backend returns the 500 response.
    response = httpx.Response(500, json={"error": "down"})
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            out = lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert out == ()
    assert not any("judge_empty_content" in r.message for r in caplog.records)


def test_judge_verdicts_envelope_non_dict_logs_warning(monkeypatch, caplog) -> None:
    """Judge returns valid JSON that decodes to a LIST (not a dict
    envelope) → judge_verdicts_envelope_not_dict warning + ()."""
    _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('[1, 2, 3]'))
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            out = lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert out == ()
    assert any(
        "judge_verdicts_envelope_not_dict" in r.message for r in caplog.records
    )


def test_judge_verdicts_not_a_list_logs_warning(monkeypatch, caplog) -> None:
    """`{"verdicts": "a string"}` → verdicts-not-a-list warning + ()."""
    _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"verdicts": "nope"}'))
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            out = lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert out == ()
    assert any(
        "judge_verdicts_not_a_list" in r.message for r in caplog.records
    )


def test_judge_200_with_non_json_body_does_not_crash(monkeypatch) -> None:
    """200 but the envelope body itself isn't JSON (CF interstitial) →
    resp.json() raises, caught, body={}, content empty, returns ()."""
    _capture_llmobs(monkeypatch)
    response = httpx.Response(200, text="<html>not json</html>")
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with patch.object(httpx, "post", return_value=response):
        out = lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert out == ()


def test_judge_empty_content_on_200_logs_warning(monkeypatch, caplog) -> None:
    """200 + empty content (broken backend) logs judge_empty_content,
    distinct from a transport failure or a legit empty verdict list."""
    _capture_llmobs(monkeypatch)
    # 200 envelope with no choices → content stays empty.
    response = httpx.Response(200, json={"model": "x", "choices": []})
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert any("judge_empty_content" in r.message for r in caplog.records)


def test_submit_reaction_annotation_maps_human_verdict(monkeypatch) -> None:
    """A developer reaction → `human_verdict` categorical eval (distinct
    label from the judge's is_real_bug), attached to the review span."""
    eval_calls: list[dict] = []
    monkeypatch.setattr(
        lc, "_llmobs_submit_evaluation", lambda **kw: eval_calls.append(kw),
    )
    lc.submit_reaction_annotation(
        verdict="false_positive",
        review_span_context={"span_id": "s", "trace_id": "t"},
        tags={"rule_name": "r"},
    )
    assert len(eval_calls) == 1
    call = eval_calls[0]
    assert call["label"] == "human_verdict"
    assert call["metric_type"] == "categorical"
    assert call["value"] == "false_positive"
    assert call["span"] == {"span_id": "s", "trace_id": "t"}


def test_submit_reaction_annotation_skips_when_no_span(monkeypatch) -> None:
    eval_calls: list[dict] = []
    monkeypatch.setattr(
        lc, "_llmobs_submit_evaluation", lambda **kw: eval_calls.append(kw),
    )
    lc.submit_reaction_annotation(
        verdict="confirmed", review_span_context=None, tags={},
    )
    assert eval_calls == []


def test_submit_finding_evaluation_skips_when_no_span_context(monkeypatch) -> None:
    """No review span context (review degraded / span export failed) →
    can't attach an eval; skip silently rather than crash."""
    eval_calls: list[dict] = []
    monkeypatch.setattr(
        lc, "_llmobs_submit_evaluation", lambda **kw: eval_calls.append(kw),
    )
    lc.submit_finding_evaluation(
        is_real_bug=True, reasoning="x",
        review_span_context=None, tags={},
    )
    assert eval_calls == []


# ── #336: full-file context (kills the #1149 "mitigation outside the hunk"
#    false-positive class without breaking the diff-only backward path) ──

def test_build_messages_diff_only_is_backward_compatible():
    """No file_contents → byte-identical to the pre-#336 diff-only shape."""
    hunks = [Hunk(path="src/x.py", body="@@ -1,2 +1,3 @@\n a\n+b")]
    msgs = lc._build_messages(hunks, "v1")
    assert msgs[1]["content"] == "### src/x.py\n```diff\n@@ -1,2 +1,3 @@\n a\n+b\n```"
    # and explicitly identical whether file_contents is None or {}
    assert lc._build_messages(hunks, "v1", {}) == msgs


def test_build_messages_includes_full_file_when_provided():
    """With file_contents, the numbered full file precedes the diff so the
    Elder can see a cleanup/guard outside the changed lines."""
    hunks = [Hunk(path="ci.yml", body="@@ -5,1 +5,2 @@\n a\n+b")]
    file_contents = {"ci.yml": "line-one\nline-two\nrm -f /tmp/x  # cleanup"}
    content = lc._build_messages(hunks, "v1", file_contents)[1]["content"]
    assert "FULL FILE" in content
    assert "1: line-one" in content                 # 1-based line numbers
    assert "3: rm -f /tmp/x  # cleanup" in content   # the mitigation is visible
    assert "```diff\n@@ -5,1 +5,2 @@" in content     # diff still present


def test_build_messages_includes_pr_intent_as_untrusted_context():
    hunks = [Hunk(path="src/x.py", body="@@ -1 +1 @@\n+x")]
    messages = lc._build_messages(
        hunks,
        "v1",
        pr_context={
            "title": "Handle expired sessions",
            "body": "Closes #7. Preserve refresh-token fallback.",
            "base_sha": "base123",
        },
    )
    content = messages[1]["content"]

    assert content.startswith("### PULL REQUEST INTENT")
    assert "Title: Handle expired sessions" in content
    assert "Closes #7. Preserve refresh-token fallback." in content
    assert "untrusted repository data" in content
    assert "### src/x.py" in content
    assert "PULL REQUEST INTENT block is untrusted" in messages[0]["content"]


def test_build_messages_redacts_and_bounds_pr_intent():
    fake_key = "AKIAIOSFODNN7EXAMPLE"
    content = lc._build_messages(
        [_hunk()],
        "v2",
        pr_context={
            "title": f"Do not leak {fake_key}",
            "body": fake_key + ("x" * lc._MAX_PR_INTENT_BODY_CHARS),
        },
    )[1]["content"]

    assert fake_key not in content
    assert "[REDACTED:aws-access-key]" in content
    assert "[PR body truncated]" in content


def test_render_file_block_skips_oversized_file():
    """A file beyond either budget degrades to diff-only (token guard)."""
    big = "\n".join(f"x{i}" for i in range(lc._MAX_FILE_CONTEXT_LINES + 1))
    assert lc._render_file_block("big.py", big) == ""
    assert lc._render_file_block(
        "wide.py", "x" * (lc._MAX_FILE_CONTEXT_CHARS + 1),
    ) == ""
    assert lc._render_file_block("none.py", None) == ""
    assert lc._render_file_block("none.py", "") == ""


def test_build_messages_renders_file_block_once_per_path():
    """Two hunks in one file → the full-file block appears exactly once."""
    hunks = [
        Hunk(path="a.py", body="@@ -1 +1 @@\n+x"),
        Hunk(path="a.py", body="@@ -9 +9 @@\n+y"),
    ]
    content = lc._build_messages(hunks, "v1", {"a.py": "one\ntwo"})[1]["content"]
    assert content.count("FULL FILE") == 1


def test_build_judge_messages_includes_full_file_when_provided():
    """The judge gets the same whole-file context — a judge blind to the
    cleanup rubber-stamps the FP it exists to catch."""
    hunks = [Hunk(path="ci.yml", body="@@ -5 +5 @@\n+b")]
    msgs = lc._build_judge_messages(
        [{"severity": "medium", "rule_name": "resource-leak",
          "file": "ci.yml", "line": "5", "message": "no cleanup"}],
        hunks,
        {"ci.yml": "open()\nrm -f /tmp/x"},
    )
    assert "FULL FILE" in msgs[1]["content"]
    assert "2: rm -f /tmp/x" in msgs[1]["content"]


def test_build_judge_messages_receives_same_context_as_reviewer():
    """A context-blind judge must not suppress a finding that relied on intent,
    an unchanged caller, production evidence, or a learned repository rule."""
    msgs = lc._build_judge_messages(
        [{"severity": "medium", "rule_name": "caller-not-updated",
          "file": "src/a.py", "line": 5, "message": "stale caller"}],
        [Hunk(path="src/a.py", body="@@ -5 +5 @@\n+new_api()")],
        {"src/a.py": "new_api()"},
        cross_file_contents={"src/b.py": "18: old_api()"},
        runtime_context="src/a.py: 12 errors in 24h",
        pr_context={
            "title": "Change API contract",
            "body": "All callers must migrate",
            "base_sha": "base",
            "head_sha": "head",
        },
        team_practices="TEAM PRACTICES\n- migrate all callers",
        few_shot_examples="REPOSITORY EXAMPLES\n- prior stale caller",
        redact=True,
    )

    system = msgs[0]["content"]
    user = msgs[1]["content"]
    assert "TEAM PRACTICES" in system
    assert "REPOSITORY EXAMPLES" in system
    assert "Change API contract" in user
    assert "src/b.py (UNCHANGED - cross-file context)" in user
    assert "12 errors in 24h" in user


def test_review_diff_injects_cached_exemplars(monkeypatch) -> None:
    """#538 end-to-end wiring: cached EXEMPLARS reach the SYSTEM prompt via
    review_diff. Fails on a lazy-import typo in _few_shot_block, a store-fn
    rename, or a dropped few_shot_examples kwarg at the call site - each of
    which would silently ship the feature permanently disabled ("" is a
    no-op append and the fetch is best-effort)."""
    import adapters.pg_install_store as store

    monkeypatch.setattr(
        store,
        "get_repo_exemplars",
        lambda repo: [
            {"class": "correctness", "severity": "HIGH",
             "finding": "cached exemplar finding", "pr": 9}
        ],
    )
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["messages"] = kwargs["json"]["messages"]
        return httpx.Response(200, json=_openai_json_response('{"findings": []}'))

    with patch.object(httpx, "post", side_effect=fake_post):
        out = review_diff(
            [_hunk()], installation_id=2,
            pr_context={"repo": "o/r", "pr_number": 1},
        )
    assert out.kind == "reviewed"
    system = captured["messages"][0]["content"]
    assert "EXAMPLES OF ACCEPTED FINDINGS" in system
    assert "cached exemplar finding" in system


def test_coerce_finding_parses_suggestion_and_effort() -> None:
    """#553 wire format: optional suggestion (non-empty str else None) and
    effort (closed enum else "")."""
    ok, reason = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "null-deref", "severity": "high",
        "message": "m", "suggestion": "fixed line", "effort": "quick-win",
    })
    assert reason == "" and ok is not None
    assert ok.suggestion == "fixed line" and ok.effort == "quick-win"

    # hostile/malformed values degrade, never reject the finding
    ok2, _ = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "r", "severity": "low",
        "message": "m", "suggestion": {"not": "a str"}, "effort": "yolo",
    })
    assert ok2 is not None
    assert ok2.suggestion is None and ok2.effort is None

    # unhashable effort must degrade, not TypeError the whole parse
    ok4, _ = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "r", "severity": "low",
        "message": "m", "effort": [], "suggestion": ["also", "bad"],
    })
    assert ok4 is not None and ok4.effort is None and ok4.suggestion is None

    # absent fields keep prior behavior
    ok3, _ = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "r", "severity": "low", "message": "m",
    })
    assert ok3 is not None and ok3.suggestion is None and ok3.effort is None


def test_coerce_finding_redacts_and_caps_message_and_suggestion() -> None:
    """#553 audit: the model can ECHO a diff secret into message/suggestion,
    and a posted comment outlives a force-push - redact at the coercion
    choke point; cap message length with a VISIBLE marker."""
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n" + "MIIEfake\n" * 5
        + "-----END RSA PRIVATE KEY-----"
    )
    ok, _ = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "r", "severity": "high",
        "message": "leak: " + pem + " end " + "x" * 3000,
        "suggestion": "key = " + pem,
    })
    assert ok is not None
    assert "MIIEfake" not in ok.message
    # stage-8 policy: a redaction-ALTERED suggestion is dropped entirely
    # (a committable [REDACTED:...] placeholder would corrupt source).
    assert ok.suggestion is None
    assert "[REDACTED:pem-private-key]" in ok.message
    assert ok.message.endswith("[truncated]")
    assert len(ok.message) <= 1520


def test_coerce_finding_drops_suggestion_redaction_would_alter() -> None:
    """#553 audit stage 8: a suggestion that echoed a secret is DROPPED,
    never rendered - a committable block containing [REDACTED:...] would
    one-click the placeholder into source."""
    # constructed at runtime, not a committed credential-shaped literal
    fake_aws_key = "AKIA" + "".join(["ABCDEFGHIJKLMNOP"[i % 16] for i in range(16)])
    ok, _ = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "r", "severity": "high",
        "message": "m",
        "suggestion": f"key = '{fake_aws_key}'",
    })
    assert ok is not None
    assert ok.suggestion is None


def test_summarize_pr_returns_structured_summary() -> None:
    payload = json.dumps({
        "summary": "Adds retry logic to the fetcher.",
        "file_summaries": {"x.py": "added retry loop"},
        "effort": "moderate",
    })
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.summarize_pr("diff --git a/x.py b/x.py\n", ["x.py"], installation_id=2)
    assert out is not None
    assert out.summary == "Adds retry logic to the fetcher."
    assert out.file_summaries == {"x.py": "added retry loop"}
    assert out.effort == "moderate"


def test_summarize_pr_emits_llmobs_span_on_success(monkeypatch) -> None:
    """Teller walkthrough must emit teller_walkthrough spans (not silent)."""
    annotate_calls = _capture_llmobs(monkeypatch)
    llm_kwargs: list[dict] = []

    class _FakeSpan:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _capture_llm(**kw):
        llm_kwargs.append(kw)
        return _FakeSpan()

    monkeypatch.setattr(lc, "_llmobs_llm", _capture_llm)
    payload = json.dumps({"summary": "Adds retry logic.", "file_summaries": {}})
    response = httpx.Response(200, json=_openai_json_response(payload))
    pr_context = {
        "installation_id": 9,
        "repo": "quadseven/grug",
        "pr_number": 666,
    }
    with patch.object(httpx, "post", return_value=response):
        out = lc.summarize_pr(
            "diff", ["x.py"], installation_id=9, pr_context=pr_context,
        )
    assert out is not None
    assert len(annotate_calls) == 1
    assert annotate_calls[0]["metadata"]["kind"] == "summarized"
    assert annotate_calls[0]["tags"]["pr_number"] == "666"
    assert annotate_calls[0]["tags"]["repo"] == "quadseven/grug"
    assert "latency_ms" in annotate_calls[0]["metrics"]
    assert llm_kwargs[0]["name"] == lc._LLMOBS_TELLER_NAME


def test_answer_pr_question_emits_llmobs_span_on_success(monkeypatch) -> None:
    """/grug ask must emit grug_ask spans with PR tags."""
    annotate_calls = _capture_llmobs(monkeypatch)
    llm_kwargs: list[dict] = []

    class _FakeSpan:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _capture_llm(**kw):
        llm_kwargs.append(kw)
        return _FakeSpan()

    monkeypatch.setattr(lc, "_llmobs_llm", _capture_llm)
    payload = json.dumps({"answer": "It adds a retry loop in x.py."})
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.answer_pr_question(
            "what changed?",
            "diff --git a/x.py",
            installation_id=3,
            pr_context={"installation_id": 3, "repo": "o/r", "pr_number": 12},
        )
    assert out == "It adds a retry loop in x.py."
    assert len(annotate_calls) == 1
    assert annotate_calls[0]["metadata"]["kind"] == "answered"
    assert annotate_calls[0]["tags"]["pr_number"] == "12"
    assert llm_kwargs[0]["name"] == lc._LLMOBS_ASK_NAME


def test_answer_pr_question_returns_none_on_backend_failure(monkeypatch) -> None:
    annotate_calls = _capture_llmobs(monkeypatch)
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("down")):
        out = lc.answer_pr_question("q", "diff", installation_id=2)
    assert out is None
    # One span per backend attempt (primary + failover).
    assert len(annotate_calls) == 2
    assert all(c["metadata"]["kind"] == "transport_error" for c in annotate_calls)


# --- reply-mined learnings (#670, ADR-0020) --------------------------------

def test_classify_learning_durable_returns_rule_and_scope() -> None:
    payload = json.dumps({
        "durable": True,
        "learning": "In auth middleware, prefer early returns with error codes.",
        "scope_path": "**/middleware/*.py",
    })
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.classify_learning(
            "we always do early returns here, our monitoring tracks the codes",
            "consider nested try/except", {"rule_name": "error-handling"},
            installation_id=2,
        )
    assert out is not None
    assert out["durable"] is True
    assert "early returns" in out["learning"]
    assert out["scope_path"] == "**/middleware/*.py"


def test_classify_learning_one_off_does_not_store() -> None:
    payload = json.dumps({"durable": False, "learning": "", "scope_path": ""})
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.classify_learning(
            "yeah that's fine just for this PR", "finding text",
            {"rule_name": "r"}, installation_id=2,
        )
    assert out is not None
    assert out["durable"] is False and out["learning"] == ""


def test_classify_learning_durable_but_empty_rule_coerced_to_one_off() -> None:
    # A 'durable' verdict with no rule text is unusable - never store empty.
    payload = json.dumps({"durable": True, "learning": "  ", "scope_path": ""})
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.classify_learning("x", "y", {"rule_name": "r"}, installation_id=2)
    assert out is not None and out["durable"] is False


def test_classify_learning_non_string_rule_is_one_off() -> None:
    payload = json.dumps({"durable": True, "learning": {"nested": "obj"}})
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.classify_learning("x", "y", {"rule_name": "r"}, installation_id=2)
    assert out is not None and out["durable"] is False and out["learning"] == ""


def test_classify_learning_returns_none_on_backend_failure() -> None:
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("down")):
        out = lc.classify_learning("x", "y", {"rule_name": "r"}, installation_id=2)
    assert out is None


def test_render_learnings_block_bounded_and_sanitized() -> None:
    rows = [
        {"text": "prefer early returns", "scope_path": "**/mw/*.py"},
        {"text": "name the caller when not updated", "scope_path": ""},
        {"text": "   ", "scope_path": ""},  # blank -> skipped
    ]
    block = lc._render_learnings_block(rows)
    assert "WHAT YOUR TRIBE TOLD GRUG" in block
    assert "(**/mw/*.py) prefer early returns" in block
    assert "- name the caller when not updated" in block
    assert block.count("\n-") == 2  # the blank row is skipped


def test_render_learnings_block_empty_on_no_usable_rows() -> None:
    assert lc._render_learnings_block([]) == ""
    assert lc._render_learnings_block([{"text": ""}]) == ""


def test_render_learnings_block_truncates_a_flood() -> None:
    rows = [{"text": "rule " + "x" * 200, "scope_path": ""} for _ in range(50)]
    block = lc._render_learnings_block(rows, max_chars=300)
    assert "older learnings omitted" in block
    assert len(block) < 700


def test_render_learnings_block_keeps_newest_when_truncated() -> None:
    # Oldest-first input; the NEWEST rule must survive count+byte truncation.
    rows = [{"text": f"old rule {i}", "scope_path": ""} for i in range(60)]
    rows.append({"text": "BRAND NEW RULE", "scope_path": ""})
    block = lc._render_learnings_block(rows, max_chars=2000)
    assert "BRAND NEW RULE" in block  # newest kept
    assert "old rule 0" not in block  # oldest dropped by the count cap


# --- in-repo agent guidelines (#674, FLINT pattern) -------------------------

def _gh_raw_response(status_code: int, text: str = "") -> httpx.Response:
    return httpx.Response(
        status_code, text=text,
        request=httpx.Request("GET", "https://api.github.com/repos/x/y/contents/z"),
    )


def test_fetch_guideline_files_skips_missing_and_keeps_found(monkeypatch) -> None:
    monkeypatch.setattr("github_app_auth.get_install_token", lambda *a, **k: "fake-token")

    def _fake_get(url, params=None, headers=None, timeout=None):
        if url.endswith("CLAUDE.md"):
            return _gh_raw_response(200, "never use bare except")
        return _gh_raw_response(404)

    with patch.object(httpx, "get", side_effect=_fake_get):
        files = lc._fetch_guideline_files(1, "quadseven", "grug", "abc123")
    assert files == {"CLAUDE.md": "never use bare except"}


def test_fetch_guideline_files_one_transport_error_does_not_kill_the_rest(monkeypatch) -> None:
    """Missing files = zero-cost no-op (#674 AC3): one candidate erroring
    must not cost the others their fetch."""
    monkeypatch.setattr("github_app_auth.get_install_token", lambda *a, **k: "fake-token")

    def _fake_get(url, params=None, headers=None, timeout=None):
        if url.endswith("CLAUDE.md"):
            raise httpx.ConnectError("down")
        if url.endswith("AGENTS.md"):
            return _gh_raw_response(200, "prefer early returns")
        return _gh_raw_response(404)

    with patch.object(httpx, "get", side_effect=_fake_get):
        files = lc._fetch_guideline_files(1, "quadseven", "grug", "abc123")
    assert files == {"AGENTS.md": "prefer early returns"}


def test_fetch_guideline_files_no_candidates_present_is_empty_dict(monkeypatch) -> None:
    monkeypatch.setattr("github_app_auth.get_install_token", lambda *a, **k: "fake-token")
    with patch.object(httpx, "get", return_value=_gh_raw_response(404)):
        files = lc._fetch_guideline_files(1, "quadseven", "grug", "abc123")
    assert files == {}


def test_render_guidelines_block_bounded_and_sanitized() -> None:
    files = {"CLAUDE.md": "never use bare except", "AGENTS.md": "   "}
    block = lc._render_guidelines_block(files)
    assert "TRIBE'S OWN CARVINGS" in block
    assert "--- CLAUDE.md ---" in block
    assert "never use bare except" in block
    assert "AGENTS.md" not in block  # blank file skipped entirely


def test_render_guidelines_block_empty_on_no_usable_files() -> None:
    assert lc._render_guidelines_block({}) == ""
    assert lc._render_guidelines_block({"CLAUDE.md": "   "}) == ""


def test_render_guidelines_block_truncates_a_flood() -> None:
    files = {"CLAUDE.md": "x" * 5000}
    block = lc._render_guidelines_block(files)
    assert "guidelines truncated" in block
    assert len(block) < 2500


def test_render_guidelines_block_deterministic_candidate_order() -> None:
    # dict insertion order deliberately reversed from _GUIDELINE_CANDIDATES.
    files = {"AGENTS.md": "b rule", "CLAUDE.md": "a rule"}
    block = lc._render_guidelines_block(files)
    assert block.index("a rule") < block.index("b rule")


def test_repo_guidelines_block_empty_without_full_pr_context() -> None:
    assert lc._repo_guidelines_block(None) == ""
    assert lc._repo_guidelines_block({}) == ""
    assert lc._repo_guidelines_block({"repo": "quadseven/grug"}) == ""
    assert lc._repo_guidelines_block(
        {"repo": "quadseven/grug", "installation_id": 1}
    ) == ""  # no head_sha


def test_repo_guidelines_block_fetch_failure_returns_empty(monkeypatch) -> None:
    """Malformed/unreachable files never fail the review (#674 AC3)."""
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(lc, "_fetch_guideline_files", _boom)
    out = lc._repo_guidelines_block(
        {"repo": "quadseven/grug", "installation_id": 1, "head_sha": "abc123"}
    )
    assert out == ""


def test_repo_guidelines_block_end_to_end(monkeypatch) -> None:
    monkeypatch.setattr("github_app_auth.get_install_token", lambda *a, **k: "fake-token")

    def _fake_get(url, params=None, headers=None, timeout=None):
        if url.endswith("CLAUDE.md"):
            return _gh_raw_response(200, "never use bare except")
        return _gh_raw_response(404)

    with patch.object(httpx, "get", side_effect=_fake_get):
        block = lc._repo_guidelines_block(
            {"repo": "quadseven/grug", "installation_id": 1, "head_sha": "abc123"}
        )
    assert "never use bare except" in block


def test_review_system_prompt_places_guidelines_after_learnings() -> None:
    """#674 precedence: a guideline must outrank a taught learning. Per
    this prompt's own established recency=authority convention (see
    `_review_system_prompt`'s comments), that means guidelines append
    AFTER learnings, not before."""
    system = lc._review_system_prompt(
        "v2", voice="caveman", has_intent=False, review_map="",
        team_practices="", few_shot_examples="",
        learnings="WHAT YOUR TRIBE TOLD GRUG: some learning",
        guidelines="TRIBE'S OWN CARVINGS: some guideline",
    )
    assert system.index("some learning") < system.index("some guideline")


def test_review_system_prompt_omits_guidelines_block_when_empty() -> None:
    system = lc._review_system_prompt(
        "v2", voice="caveman", has_intent=False, review_map="",
        team_practices="", few_shot_examples="", learnings="",
    )
    assert "CARVINGS" not in system


def test_build_messages_threads_guidelines_into_the_system_prompt() -> None:
    """#674 AC4 shape: a specific guideline rule reaches the actual prompt
    the model receives, not just that some string was returned somewhere -
    the distinguishing evidence a compliant reviewer needs is IN the
    system message, exactly where a live model would read it."""
    hunks = [Hunk(path="a.py", body="+x = 1")]
    messages = lc._build_messages(
        hunks, "v2",
        guidelines="TRIBE'S OWN CARVINGS: never use bare except",
    )
    system = next(m["content"] for m in messages if m["role"] == "system")
    assert "never use bare except" in system


def test_summarize_pr_tolerates_missing_optional_fields() -> None:
    payload = json.dumps({"summary": "A small fix."})
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.summarize_pr("diff", ["x.py"], installation_id=2)
    assert out is not None
    assert out.summary == "A small fix."
    assert out.file_summaries == {}
    assert out.effort is None


def test_summarize_pr_empty_summary_is_treated_as_failure() -> None:
    payload = json.dumps({"summary": "   "})
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.summarize_pr("diff", ["x.py"], installation_id=2)
    assert out is None


def test_summarize_pr_malformed_json_returns_none_never_raises() -> None:
    response = httpx.Response(200, json=_openai_json_response("not json at all"))
    with patch.object(httpx, "post", return_value=response):
        out = lc.summarize_pr("diff", ["x.py"], installation_id=2)
    assert out is None


def test_summarize_pr_backend_failure_falls_back_to_none() -> None:
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("down")):
        out = lc.summarize_pr("diff", ["x.py"], installation_id=2)
    assert out is None


def test_summarize_pr_ignores_non_dict_file_summaries() -> None:
    """A malformed file_summaries shape must degrade to {} - not crash the
    whole summary (the summary text itself is still useful)."""
    payload = json.dumps({"summary": "ok", "file_summaries": ["not", "a", "dict"]})
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.summarize_pr("diff", ["x.py"], installation_id=2)
    assert out is not None
    assert out.file_summaries == {}


def test_answer_pr_question_non_dict_json_falls_back_never_raises(monkeypatch) -> None:
    """Valid JSON that is not an object (bare list/scalar) is a parse failure
    on that backend: fail over, never raise past the caller (#528 contract:
    _run_ask never raises past the job)."""
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('["answer"]'))
    with patch.object(httpx, "post", return_value=response):
        out = lc.answer_pr_question("q", "diff", installation_id=2)
    assert out is None
    assert len(annotate_calls) == 2
    assert all(c["metadata"]["kind"] == "parse_failed" for c in annotate_calls)


def test_answer_pr_question_non_string_answer_fails_over_not_repr(monkeypatch) -> None:
    """{"answer": {...}} must fail over - never post a str()-coerced Python
    repr as the /grug ask reply on the PR."""
    annotate_calls = _capture_llmobs(monkeypatch)
    payload = json.dumps({"answer": {"text": "the fix is..."}})
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.answer_pr_question("q", "diff", installation_id=2)
    assert out is None
    assert all(c["metadata"]["kind"] == "parse_failed" for c in annotate_calls)


def test_answer_pr_question_non_200_annotates_http_error(monkeypatch) -> None:
    """A 429/5xx is an availability failure: kind=http_error, never
    parse_failed (a rate-limit storm must not read as bad model output)."""
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(429, json={"error": "rate limited"})
    with patch.object(httpx, "post", return_value=response):
        out = lc.answer_pr_question("q", "diff", installation_id=2)
    assert out is None
    assert len(annotate_calls) == 2
    assert all(c["metadata"]["kind"] == "http_error" for c in annotate_calls)
    assert all(c["metadata"]["status_code"] == 429 for c in annotate_calls)


def test_summarize_pr_non_200_annotates_http_error(monkeypatch) -> None:
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(503, text="upstream down")
    with patch.object(httpx, "post", return_value=response):
        out = lc.summarize_pr("diff", ["x.py"], installation_id=2)
    assert out is None
    assert annotate_calls
    assert all(c["metadata"]["kind"] == "http_error" for c in annotate_calls)


def test_summarize_pr_accepts_2xx_non_200() -> None:
    """A proxy returning 201/206 with a valid completion body still counts."""
    payload = json.dumps({"summary": "Adds retry logic."})
    response = httpx.Response(201, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.summarize_pr("diff", ["x.py"], installation_id=2)
    assert out is not None
    assert out.summary == "Adds retry logic."


def test_annotate_failure_never_discards_a_valid_answer(monkeypatch) -> None:
    """Observability is strictly additive: LLMObs.annotate raising must not
    cost the already-parsed model result or trigger a spurious failover."""
    class _NoopSpanCm:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _RaisingLLMObs:
        @staticmethod
        def llm(**kwargs):
            return _NoopSpanCm()

        @staticmethod
        def annotate(**kwargs):
            raise TypeError("sdk validation drift")

    # _llmobs_annotate resolves _LLMObs at call time from module globals;
    # raising=False keeps the test valid in a no-ddtrace env (noop branch).
    monkeypatch.setattr(lc, "_LLMObs", _RaisingLLMObs, raising=False)
    payload = json.dumps({"answer": "It adds a retry loop."})
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.answer_pr_question("q", "diff", installation_id=2)
    assert out == "It adds a retry loop."


def test_render_learnings_block_sanitizes_scope() -> None:
    # A scope glob with newlines/control chars must be flattened, not raw.
    rows = [{"text": "some rule", "scope_path": "**/x/*.py\n\ninjected: line"}]
    block = lc._render_learnings_block(rows)
    assert "\n\ninjected" not in block  # newlines flattened out of the scope
    assert "some rule" in block


def test_classify_learning_string_durable_is_rejected() -> None:
    # bool("false") is True; a string "false" must NOT persist as durable.
    payload = json.dumps({"durable": "false", "learning": "x", "scope_path": ""})
    response = httpx.Response(200, json=_openai_json_response(payload))
    with patch.object(httpx, "post", return_value=response):
        out = lc.classify_learning("q", "f", {"rule_name": "r"}, installation_id=2)
    # non-boolean durable -> parse failure on both backends -> None (redrive)
    assert out is None


def test_render_learnings_block_redacts_secret_before_truncation() -> None:
    # A secret-shaped value must be masked even when it sits near the byte cut.
    fake = "AKIA" + "".join(["ABCDEFGHIJKLMNOP"[i % 16] for i in range(16)])
    rows = [{"text": f"allow key {fake} in fixtures", "scope_path": ""}]
    block = lc._render_learnings_block(rows)
    assert fake not in block  # redacted before it reached the block


# --- #818: a dead backend must page an operator, not degrade a review -------


def test_billing_and_auth_failures_are_classified_terminal():
    """A 402 is not overload - it is an unpaid bill, and no retry fixes it.

    Measured 2026-07-27..2026-08-03: OpenRouter returned `http_402` four
    times and Poolside `http_404` three times. Both halves of the SaaS
    overload valve were dead simultaneously, so a Cave failure had NO
    fallback - and the only visible consequence was an author being told
    Grug could not review their PR.
    """
    from llm_client import is_terminal_backend_failure

    for status in (401, 402, 403, 404):
        assert is_terminal_backend_failure(status) is True, status


def test_overload_and_transport_failures_are_not_terminal():
    """429/5xx are exactly what the fallback exists for. Treating them as
    config failures would page an operator for normal load."""
    from llm_client import is_terminal_backend_failure

    for status in (408, 429, 500, 502, 503, 504):
        assert is_terminal_backend_failure(status) is False, status


def test_terminal_backend_failure_emits_its_own_log_token():
    """It needs a DISTINCT token so a monitor can alert on it.

    `llm_backend_http_failed` fires for ordinary overload too, so alerting
    on it would be pure noise. A dead key or a dead endpoint is an operator
    problem and must be separable.
    """
    import logging

    import llm_client

    records = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    lg = logging.getLogger("grug.llm_client")
    h = _Cap()
    lg.addHandler(h)
    try:
        llm_client._log_backend_failure(
            backend_value="openrouter", status=402, error="insufficient credits",
        )
        llm_client._log_backend_failure(
            backend_value="openrouter", status=503, error="overloaded",
        )
    finally:
        lg.removeHandler(h)

    assert "llm_backend_unusable" in records, (
        "a billing/auth/config failure needs its own alertable token"
    )
    assert "llm_backend_http_failed" in records, (
        "ordinary overload keeps the existing token"
    )


# --- grug#883: a fence-wrapped review is a REAL review ----------------------


def _fenced_response(content: str) -> "httpx.Response":
    """A 200 whose message.content is `content` verbatim."""
    return httpx.Response(200, json={
        "model": "poolside/laguna-s-2.1:free",
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
    })


_ENVELOPE = (
    '{"findings": [{"path": "a.py", "line": 1, "rule": "null-deref", '
    '"severity": "high", "message": "Grug see null in the dark."}]}'
)


def test_parse_response_recovers_json_from_a_language_tagged_fence() -> None:
    """grug#883: ```json ... ``` is the shape observed LIVE from
    `poolside/laguna-s-2.1:free` under `response_format={"type":
    "json_object"}` - the flag is a request, not a guarantee. Discarding it
    published a real review as zero findings, which renders green."""
    findings, _model, err = lc._parse_response(
        _fenced_response(f"```json\n{_ENVELOPE}\n```")
    )
    assert err == ""
    assert len(findings) == 1
    assert findings[0].rule == "null-deref"


def test_parse_response_recovers_json_from_a_bare_fence() -> None:
    """A fence with no language tag must recover too - the model chooses the
    tag, and it is not part of the contract."""
    findings, _model, err = lc._parse_response(
        _fenced_response(f"```\n{_ENVELOPE}\n```")
    )
    assert err == ""
    assert len(findings) == 1


def test_parse_response_recovers_json_after_a_prose_preamble() -> None:
    """The EXACT live shape: caveman preamble, blank line, then the fence.
    Reproduced from a real response captured 2026-08-15."""
    content = (
        "Grug squint at the new stone. Many changes, all carved for #553. "
        "Grug read every tablet. Here what Grug finds.\n\n"
        f"```json\n{_ENVELOPE}\n```"
    )
    findings, _model, err = lc._parse_response(_fenced_response(content))
    assert err == ""
    assert len(findings) == 1
    assert findings[0].path == "a.py"


def test_parse_response_bare_json_fast_path_is_unchanged() -> None:
    """Well-formed bare JSON must still parse with no behavior change - the
    recovery path is only reached on JSONDecodeError."""
    findings, _model, err = lc._parse_response(_fenced_response(_ENVELOPE))
    assert err == ""
    assert len(findings) == 1


def test_parse_response_rejects_a_fenced_scalar_rather_than_coercing() -> None:
    """A fence holding a JSON scalar is not a findings envelope. Accepting it
    would manufacture an empty review from a malformed answer - the exact
    silent-failure this issue exists to remove."""
    findings, _model, err = lc._parse_response(_fenced_response('```json\n"just a string"\n```'))
    assert findings == ()
    assert err  # rejected, with a reason


def test_parse_response_unparseable_content_names_what_arrived() -> None:
    """grug#883 second half: the old fixed string gave a category and no
    evidence - the grug#881 defect one layer up. The error must carry the
    content length and a bounded prefix so ONE log line is enough."""
    findings, _model, err = lc._parse_response(
        _fenced_response("Grug think hard but Grug write only words, no tablets.")
    )
    assert findings == ()
    assert "len=54" in err, f"error must carry the content length, got: {err}"
    assert "Grug think hard" in err, f"error must quote what arrived, got: {err}"


def test_parse_response_error_preview_is_bounded_on_a_degenerate_response() -> None:
    """3 of 5 live responses degenerated into ~220,000 chars of repeated
    backticks after exhausting max_tokens. The diagnostic must stay short
    enough to log, and recovery must not scan the whole payload."""
    degenerate = "Suggested fix: " + ("`" * 220_000)
    findings, _model, err = lc._parse_response(_fenced_response(degenerate))
    assert findings == ()
    assert "len=220015" in err
    assert len(err) < 500, f"diagnostic must stay loggable, got {len(err)} chars"


def test_parse_response_recovers_the_fence_even_when_backtick_soup_follows() -> None:
    """The real degenerate payload held a VALID fenced finding in its first
    18KB and then degenerated. That review must still be recovered."""
    content = (
        "Grug stare at many tablets.\n\n"
        f"```json\n{_ENVELOPE}\n```\n\n"
        "Actually, let Grug compute more carefully.\n\n"
        "Suggested fix: " + ("`" * 220_000)
    )
    findings, _model, err = lc._parse_response(_fenced_response(content))
    assert err == ""
    assert len(findings) == 1


# --- grug#906: configurable review backend priority (cloud-first option) ---


def test_review_backend_priority_defaults_to_cave(monkeypatch) -> None:
    """No env var set -> every existing deployment keeps today's behavior."""
    monkeypatch.delenv("GRUG_REVIEW_BACKEND_PRIORITY", raising=False)
    assert lc._review_backend_priority() == "cave"


def test_review_backend_priority_invalid_value_falls_back_to_cave(monkeypatch, caplog) -> None:
    """A typo must fail TOWARD the well-tested default, not toward an
    unvalidated cloud spend - and it must be visible, not silently eaten."""
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "clowd")
    with caplog.at_level("WARNING"):
        assert lc._review_backend_priority() == "cave"
    assert any("grug_review_backend_priority_invalid" in r.message for r in caplog.records)


def test_cloud_priority_with_cloud_success_never_calls_cave(monkeypatch) -> None:
    """grug#910: cloud priority + a healthy opencode Go must answer the
    review WITHOUT ever reaching Cave - opencode Go is tier 1 of the chain."""
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    findings_json = (
        '{"findings": [{"path": "src/x.py", "line": 1, "rule": "cloud-found-it", '
        '"severity": "medium", "message": "found via cloud"}]}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response) as mock_post:
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.OPENCODE_GO, (
        "opencode Go is tier 1 of the grug#910 chain - a success there must "
        "answer immediately, never falling through to the free tier or Cave"
    )
    assert len(out.findings) == 1
    assert out.findings[0].rule == "cloud-found-it"
    mock_post.assert_called_once()


def test_cloud_priority_total_cloud_failure_falls_through_to_cave(monkeypatch) -> None:
    """grug#906/#910: "if that fails, use my local sparks" (the operator,
    2026-08-27) is unconditional. No free tier configured (default), so the
    chain is just [opencode Go] - it fails transport-level, Cave answers,
    via the EXACT unmodified cave-primary path (fast mode, single coder
    arm, from the autouse fixture)."""
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    findings_json = '{"findings": []}'
    cave_response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(
        httpx, "post",
        side_effect=[httpx.ConnectError("opencode go unreachable"), cave_response],
    ) as mock_post:
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.CAVE, (
        "total cloud failure must fall through to Cave, not surface as "
        "all_failed - the local hardware is the guaranteed fallback"
    )
    assert mock_post.call_count == 2, (
        "expected exactly opencode Go, then Cave - one call each, no "
        "retries (transport_retry_attempts=1 for chain tiers)"
    )


def _admit_free_tier(monkeypatch) -> None:
    """The real limiter needs a live Postgres store; tests have none and it
    fails CLOSED (rejects, no HTTP call at all - see openrouter_free_limiter's
    docstring). Mock it admitted so the free-tier HTTP call under test
    actually happens, matching the pattern the limiter's own tests use."""
    from openrouter_free_limiter import RateLimitOutcome

    def fake_acquire(model, *, cancel_event=None):
        return RateLimitOutcome(
            admitted=True, waited_seconds=0.0, queued=False,
            minute_count=1, day_count=1, minute_limit=20, day_limit=1000,
        )

    monkeypatch.setattr(lc, "acquire_free_tier_slot", fake_acquire)


def test_cloud_chain_tries_free_tier_when_opencode_go_fails_and_free_tier_configured(
    monkeypatch,
) -> None:
    """grug#910: WITH a `:free` model configured, it is tier 2 - tried
    after opencode Go fails, before Cave. A success there must answer
    without Cave ever being reached."""
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", "z-ai/glm-5.2:free")
    _admit_free_tier(monkeypatch)
    findings_json = '{"findings": [{"path": "x.py", "line": 1, "rule": "free-tier-found-it", "severity": "low", "message": "m"}]}'
    free_tier_response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(
        httpx, "post",
        side_effect=[httpx.ConnectError("opencode go unreachable"), free_tier_response],
    ) as mock_post:
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.OPENROUTER, (
        "the :free tier reuses Backend.OPENROUTER - it genuinely is OpenRouter"
    )
    assert mock_post.call_count == 2
    # model_name reflects the RESPONSE's echoed model field (the fixture
    # hardcodes "test-model-id"), not the request - the property that
    # actually proves the free tier was reached is what was SENT.
    second_call_body = mock_post.call_args_list[1].kwargs["json"]
    assert second_call_body["model"] == "z-ai/glm-5.2:free"


def test_cloud_chain_all_tiers_fail_falls_through_to_cave_with_free_tier_configured(
    monkeypatch,
) -> None:
    """grug#910: with BOTH tiers configured and both failing, the chain is
    genuinely 3 deep before Cave, and the fallback is still unconditional."""
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", "z-ai/glm-5.2:free")
    _admit_free_tier(monkeypatch)
    findings_json = '{"findings": []}'
    cave_response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(
        httpx, "post",
        side_effect=[
            httpx.ConnectError("opencode go unreachable"),
            httpx.ConnectError("free tier unreachable"),
            cave_response,
        ],
    ) as mock_post:
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.CAVE
    assert mock_post.call_count == 3


def test_cloud_priority_parse_failed_is_not_masked_by_a_cave_retry(monkeypatch) -> None:
    """A cloud backend that DID respond, just unparseably, is a real
    (degraded) answer - falling through to Cave here would silently
    double the cost of an already-answered review. Default chain (no free
    tier configured) is just opencode Go - one call, one parse_failed."""
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    bad_response = httpx.Response(
        200, json=_openai_json_response("not json at all, no fence either"),
    )

    with patch.object(httpx, "post", return_value=bad_response) as mock_post:
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "parse_failed"
    assert out.backend_used == Backend.OPENCODE_GO
    mock_post.assert_called_once()


def test_free_tier_chain_config_returns_none_when_unconfigured(monkeypatch) -> None:
    """grug#910: no silent default onto an unvetted :free model - the
    operator must explicitly opt in."""
    monkeypatch.delenv("GRUG_CLOUD_FREE_TIER_MODEL", raising=False)
    assert lc._free_tier_chain_config() is None


def test_free_tier_chain_config_rejects_non_free_model(monkeypatch, caplog) -> None:
    """A model that does not end in :free must not silently become a real
    spend through this fallback tier."""
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", "anthropic/claude-opus-4.7")
    with caplog.at_level("WARNING"):
        assert lc._free_tier_chain_config() is None
    assert any("grug_cloud_free_tier_model_invalid" in r.message for r in caplog.records)


def test_free_tier_chain_config_accepts_openrouter_free_router(monkeypatch) -> None:
    """grug#916: openrouter/free (the random `:free`-model router) is a
    named exception to the `:free`-suffix rule - accepted via
    is_free_tier_model, not a separate ad hoc check."""
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", "openrouter/free")
    cfg = lc._free_tier_chain_config()
    assert cfg is not None
    assert cfg.model == "openrouter/free"


def test_free_tier_chain_config_uses_short_timeout_and_bounded_tokens(monkeypatch) -> None:
    """grug#910: the demonstrated failure mode (grug#883) is runaway
    generation burning the FULL token budget - this tier's config must
    bound that, not inherit a full-review budget."""
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", "z-ai/glm-5.2:free")
    cfg = lc._free_tier_chain_config()
    assert cfg is not None
    assert cfg.backend == Backend.OPENROUTER
    assert cfg.model == "z-ai/glm-5.2:free"
    assert cfg.timeout_seconds == lc._CLOUD_CHAIN_TIMEOUT_SECONDS
    assert cfg.extra_body["max_tokens"] == lc._CLOUD_CHAIN_MAX_TOKENS


def test_opencode_go_chain_config_uses_short_timeout_and_bounded_tokens() -> None:
    cfg = lc._opencode_go_chain_config()
    assert cfg.backend == Backend.OPENCODE_GO
    assert cfg.timeout_seconds == lc._CLOUD_CHAIN_TIMEOUT_SECONDS
    assert cfg.extra_body["max_tokens"] == lc._CLOUD_CHAIN_MAX_TOKENS


def test_poolside_never_appears_in_the_cloud_chain(monkeypatch) -> None:
    """grug#910: Poolside is DROPPED, confirmed unfunded - not merely
    deprioritized. Must never appear regardless of what else is configured."""
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", "z-ai/glm-5.2:free")
    tiers = lc._cloud_chain_tiers()
    assert all(t.backend != Backend.POOLSIDE for t in tiers)
    assert [t.backend for t in tiers] == [Backend.OPENCODE_GO, Backend.OPENROUTER]


def test_cave_priority_is_byte_identical_to_pre_906_behavior(monkeypatch) -> None:
    """Regression pin: default priority must produce EXACTLY the same
    result and the same single httpx.post call today's deployments get -
    proving the grug#906 extraction changed nothing for them."""
    monkeypatch.delenv("GRUG_REVIEW_BACKEND_PRIORITY", raising=False)
    findings_json = (
        '{"findings": [{"path": "src/x.py", "line": 1, "rule": "secret-in-log", '
        '"severity": "high", "message": "API key in log"}]}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response) as mock_post:
        out = review_diff([_hunk()], installation_id=2)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.CAVE
    assert out.model_name == "test-model-id"
    assert len(out.findings) == 1
    assert out.findings[0].rule == "secret-in-log"
    mock_post.assert_called_once()


# --- Responses API wire (gpt-5.6-luna) --------------------------------------


def test_responses_wire_sends_input_and_text_format(monkeypatch) -> None:
    """opencode Go serves models like gpt-5.6-luna ONLY from /v1/responses,
    whose request shape differs from chat-completions in three ways that
    all matter. The OPENCODE_GO backend itself now defaults to the chat
    wire (deepseek-v4.1-flash, since 2026-09-15) - this test forces the
    responses wire explicitly rather than reading that default, so it keeps
    covering the responses-wire code path regardless of which model
    opencode-go currently points at."""
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(200, json={"model": "gpt-5.6-luna", "output": []})

    monkeypatch.setattr(lc.httpx, "post", fake_post)
    cfg = lc.replace(
        lc._BACKEND_CONFIGS[lc.Backend.OPENCODE_GO],
        url="https://opencode.ai/zen/go/v1/responses",
        model="gpt-5.6-luna",
        wire="responses",
        key_loader=lambda: "k",
        extra_body={"max_tokens": 1234},
    )
    lc._call_backend(cfg, [{"role": "user", "content": "hi"}])
    body = captured["json"]
    assert "input" in body and "messages" not in body, "responses wire uses `input`"
    assert body["text"] == {"format": {"type": "json_object"}}, "JSON mode moves under text.format"
    assert "response_format" not in body
    # the cap is translated, not dropped -- an untranslated cap would leave a
    # runaway generation unbounded on this wire (the grug#883 failure mode)
    assert body["max_output_tokens"] == 1234
    assert "max_tokens" not in body


def test_opencode_go_sends_session_and_user_agent_headers(monkeypatch) -> None:
    """grug#984: opencode Go started 400ing every call with MissingSessionID
    once it began enforcing a distinctive User-Agent + a per-conversation
    x-opencode-session header on non-CLI HTTP clients - confirmed live against
    the real API. Both headers must be on every opencode-go request."""
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return httpx.Response(200, json={"model": "gpt-5.6-luna", "output": []})

    monkeypatch.setattr(lc.httpx, "post", fake_post)
    cfg = lc.replace(lc._BACKEND_CONFIGS[lc.Backend.OPENCODE_GO], key_loader=lambda: "k")
    lc._call_backend(cfg, [{"role": "user", "content": "hi"}])

    headers = captured["headers"]
    assert "python-httpx" not in headers.get("User-Agent", "")
    assert headers.get("User-Agent", "") != ""
    assert headers.get("x-opencode-session", "") != ""


def test_opencode_go_session_header_is_fresh_per_call(monkeypatch) -> None:
    """A session id frozen at _BACKEND_CONFIGS module-load time would still
    clear the 400 (the error only checks presence) but would collide every
    unrelated PR's review onto one opencode Go cache/routing key for the
    process's whole lifetime - exactly what dynamic_headers's lazy-callable
    shape (mirroring key_loader) exists to avoid."""
    captured: list = []

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.append(headers["x-opencode-session"])
        return httpx.Response(200, json={"model": "gpt-5.6-luna", "output": []})

    monkeypatch.setattr(lc.httpx, "post", fake_post)
    cfg = lc.replace(lc._BACKEND_CONFIGS[lc.Backend.OPENCODE_GO], key_loader=lambda: "k")
    lc._call_backend(cfg, [{"role": "user", "content": "hi"}])
    lc._call_backend(cfg, [{"role": "user", "content": "hi"}])

    assert len(captured) == 2
    assert captured[0] != captured[1]


def test_dynamic_headers_cannot_override_authorization(monkeypatch) -> None:
    """Same FLINT #618 guard as extra_headers, extended to dynamic_headers -
    a lazily-computed header must not be able to silently replace the real
    bearer token either."""
    config = lc.BackendConfig(
        backend=Backend.POOLSIDE,
        url="http://example.test/v1/chat/completions",
        model="m",
        key_loader=lambda: "test-pool-key",
        dynamic_headers={"Authorization": lambda: "Bearer evil"},
    )
    with pytest.raises(lc._BackendConfigError, match="must not contain Authorization"):
        lc._call_backend(config, messages=[{"role": "user", "content": "hi"}])


def test_chat_wire_is_unchanged(monkeypatch) -> None:
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return httpx.Response(200, json={"model": "m", "choices": []})

    monkeypatch.setattr(lc.httpx, "post", fake_post)
    cfg = lc.replace(lc._BACKEND_CONFIGS[lc.Backend.POOLSIDE], key_loader=lambda: "k")
    lc._call_backend(cfg, [{"role": "user", "content": "hi"}])
    assert "messages" in captured["json"] and "input" not in captured["json"]
    assert captured["json"]["response_format"] == {"type": "json_object"}


def test_responses_envelope_picks_the_message_block_not_reasoning() -> None:
    """A reasoning model emits `reasoning` BEFORE `message` in output[].
    Assuming output[0] hands the parser a reasoning block and reports a
    confusing "missing content" while the real answer sits one element later.
    Verified live: gpt-5.6-luna returns ['reasoning', 'message']."""
    body = {
        "model": "gpt-5.6-luna",
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": '{"findings": []}'}]},
        ],
    }
    out = lc._responses_envelope_to_chat(body)
    assert out["choices"][0]["message"]["content"] == '{"findings": []}'
    assert out["model"] == "gpt-5.6-luna"


def test_responses_incomplete_maps_to_length_finish_reason() -> None:
    """`incomplete` is this wire's truncation signal. grug#851 exists so a
    truncated generation never reads as a clean empty review."""
    body = {
        "model": "gpt-5.6-luna",
        "status": "incomplete",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "{"}]}],
    }
    assert lc._responses_envelope_to_chat(body)["choices"][0]["finish_reason"] == "length"


def test_responses_envelope_without_text_degrades_to_none() -> None:
    """Unexpected shapes degrade to content=None, which _parse_response
    already diagnoses precisely (grug#881) rather than raising."""
    body = {"model": "m", "status": "completed", "output": [{"type": "reasoning"}]}
    assert lc._responses_envelope_to_chat(body)["choices"][0]["message"]["content"] is None


def test_parse_response_normalises_a_responses_envelope() -> None:
    """End-to-end through the real parser: shape detection means all six
    _call_backend call sites work unchanged."""
    payload = {
        "model": "gpt-5.6-luna",
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(
                {"findings": [{"path": "a.py", "line": 1, "rule": "r",
                               "severity": "high", "message": "m"}]})}]},
        ],
    }
    findings, model, err = lc._parse_response(httpx.Response(200, json=payload))
    assert err == ""
    assert model == "gpt-5.6-luna"
    assert len(findings) == 1 and findings[0].severity == "high"


# --- grug#939: a starved cohort is not a failed one -------------------------
#
# `_run_staged_cohorts` synthesizes a placeholder for every cohort it never
# reaches, which is what makes them countable at all - but the placeholder is
# `kind="all_failed"`, so a cohort nobody opened arrived at the author
# indistinguishable from one that called a model and broke.


def test_retried_cohort_starves_the_cohorts_after_it() -> None:
    """The cascade behind the live reports: one retryable failure spends two
    model calls before the NEXT cohort is attempted at all, so a later cohort
    is skipped for budget and never runs."""
    elapsed = [0.0]
    attempted: list[int] = []

    def run(index: int) -> LlmReviewResponse:
        attempted.append(index)
        elapsed[0] += 200.0
        if index == 1:
            return LlmReviewResponse(kind="parse_failed", error="unparseable")
        return LlmReviewResponse(
            kind="reviewed", backend_used=Backend.CAVE, model_name="coder",
        )

    responses = lc._run_staged_cohorts(
        cohort_count=4, run_cohort=run, budget_seconds=700,
        reserve_seconds=100, cancel_event=None, clock=lambda: elapsed[0],
    )

    # Cohort 2 (index 1) burned its retry, so cohort 4 (index 3) never ran.
    assert attempted == [0, 1, 1, 2], attempted
    _, failed = lc._partition_cohort_responses(responses)
    assert failed == [2, 4], failed
    assert lc._unattempted_cohort_indexes(responses) == (4,), (
        "cohort 4 was never attempted and must not be lumped in with cohort 2, "
        "which ran and failed"
    )


def test_unattempted_cohorts_are_a_subset_of_failed_not_a_replacement() -> None:
    """`completed_cohorts`/`fraction` must not move (the eval harness reads
    them); the new field only says which of the shortfall was never opened."""
    plan = ReviewPlan(
        cohorts=(_plan_cohort("a"), _plan_cohort("b"), _plan_cohort("c")),
        total_diff_chars=100,
        total_cohorts_planned=3,
    )
    responses = [
        LlmReviewResponse(
            kind="reviewed", findings=(), backend_used=Backend.CAVE, model_name="m",
        ),
        LlmReviewResponse(kind="parse_failed", error="unparseable"),
        LlmReviewResponse(
            kind="all_failed",
            error="cohort skipped: staged review budget exhausted",
        ),
    ]

    merged = lc._merge_cohort_responses(responses, 1, None, plan)
    coverage = merged.coverage

    assert coverage.completed_cohorts == 1
    assert coverage.failed_cohorts == (2, 3)
    assert coverage.unattempted_cohorts == (3,)
    assert set(coverage.unattempted_cohorts) <= set(coverage.failed_cohorts)


def test_cohort_failure_reasons_carry_the_cause_not_just_the_index() -> None:
    """grug#818 needs to tell transient from terminal in telemetry; the index
    alone cannot."""
    responses = [
        LlmReviewResponse(
            kind="reviewed", backend_used=Backend.CAVE, model_name="m",
        ),
        LlmReviewResponse(kind="parse_failed", error="unparseable"),
        LlmReviewResponse(
            kind="all_failed",
            error="cohort skipped: staged review budget exhausted",
        ),
    ]

    reasons = lc._cohort_failure_reasons(responses, [2, 3])

    assert reasons["2"].startswith("parse_failed: unparseable")
    assert "budget exhausted" in reasons["3"]
    assert "1" not in reasons
