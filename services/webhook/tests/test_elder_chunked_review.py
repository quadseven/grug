"""grug#1196: Elder reviews a failing diff in chunks instead of ending neutral.

Live 2026-10-17: Elder ended "eyes clouded (all_failed)" or "(parse_failed)"
on large pull requests and once on a two-line one. Three gaps:

1. A failed multi-file chunk was never retried with a smaller input.
2. The cohort retry walked the same model ladder again from the top, and the
   neutral title named no model and no reason.
3. A valid findings object wrapped in prose counted as a parse failure.

Plus the check summary said how many cohorts completed, never which files.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import llm_client as lc
import pytest
from llm_client import Backend, Hunk, LlmReviewResponse, review_diff
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.persona import CodeReviewEvaluation, evaluate_diff
from review_pipeline import ReviewCoverage
from review_evidence import assemble_evidence


@pytest.fixture(autouse=True)
def _owned_ladder(monkeypatch):
    """Cave-first ladder in fast depth: coder, reasoner, then the SaaS valve."""
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "test-ocg-key")
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda _s: None)
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    monkeypatch.delenv("GRUG_REVIEW_BACKEND_PRIORITY", raising=False)
    monkeypatch.delenv("GRUG_REVIEW_COHORT_CHARS", raising=False)


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "model": "test-model-id",
        },
    )


def _finding(path: str, rule: str) -> dict:
    return {
        "path": path, "line": 1, "rule": rule,
        "severity": "medium", "message": f"{rule} here",
    }


def _cave_models() -> tuple[str, str]:
    return (
        lc._review_backend_config(Backend.CAVE).model,
        lc._review_backend_config(Backend.CAVE_REASONER).model,
    )


def test_a_failed_two_file_diff_is_reviewed_one_file_at_a_time() -> None:
    """Every model fails on the whole diff; each file alone reviews fine.
    The markings from both file chunks land in one result."""
    hunks = [
        Hunk(path="src/a.py", body="@@ -1 +1 @@\n+A_CHANGE"),
        Hunk(path="src/b.py", body="@@ -1 +1 @@\n+B_CHANGE"),
    ]
    prompts: list[str] = []

    def respond(_url, **kwargs) -> httpx.Response:
        prompt = kwargs["json"]["messages"][1]["content"]
        prompts.append(prompt)
        if "A_CHANGE" in prompt and "B_CHANGE" in prompt:
            raise httpx.ReadTimeout("whole diff too slow")
        if "A_CHANGE" in prompt:
            return _completion(json.dumps({"findings": [_finding("src/a.py", "a-bug")]}))
        return _completion(json.dumps({"findings": [_finding("src/b.py", "b-bug")]}))

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff(assemble_evidence(hunks), installation_id=2)

    assert out.kind == "reviewed", out.error
    assert {finding.rule for finding in out.findings} == {"a-bug", "b-bug"}
    assert out.error == ""
    assert out.coverage is not None
    assert out.coverage.reviewed_paths == ("src/a.py", "src/b.py")
    assert out.coverage.unwalked_paths == ()
    single_file = [p for p in prompts if ("A_CHANGE" in p) != ("B_CHANGE" in p)]
    assert len(single_file) == 2, "each file chunk is one model call"


def test_a_file_chunk_that_still_fails_is_named_as_not_reviewed() -> None:
    hunks = [
        Hunk(path="src/a.py", body="@@ -1 +1 @@\n+A_CHANGE"),
        Hunk(path="src/b.py", body="@@ -1 +1 @@\n+B_CHANGE"),
    ]

    def respond(_url, **kwargs) -> httpx.Response:
        prompt = kwargs["json"]["messages"][1]["content"]
        if "B_CHANGE" in prompt:
            raise httpx.ReadTimeout("b is cursed")
        return _completion(json.dumps({"findings": [_finding("src/a.py", "a-bug")]}))

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff(assemble_evidence(hunks), installation_id=2)

    assert out.kind == "reviewed"
    assert [finding.rule for finding in out.findings] == ["a-bug"]
    assert out.error.startswith("partial review:")
    assert out.coverage is not None
    assert out.coverage.reviewed_paths == ("src/a.py",)
    assert out.coverage.unwalked_paths == ("src/b.py",)


def test_retry_moves_to_the_next_model_and_the_title_names_every_failure() -> None:
    """The coder answers in prose, so the first walk stops before the SaaS
    valve (a parse failure is not an outage). The retry skips both Cave
    models and tries the next ones in the ladder exactly once. The neutral
    title then names each model and its reason."""
    coder, reasoner = _cave_models()
    calls: list[str] = []

    def respond(_url, **kwargs) -> httpx.Response:
        model = kwargs["json"]["model"]
        calls.append(model)
        if model == coder:
            return _completion("Grug looks at the diff and sees no JSON today.")
        if model == reasoner:
            raise httpx.ReadTimeout("reasoner slow")
        if model == lc._POOLSIDE_MODEL:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        raise httpx.ConnectError("down")

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff(
            assemble_evidence([Hunk(path="infra/x.tf", body="@@ -1 +1 @@\n+x")]),
            installation_id=2,
        )

    assert out.kind == "parse_failed"
    assert out.backend_used == Backend.CAVE, "the first unparseable answer keeps attribution"
    assert calls.count(coder) == 1 and calls.count(reasoner) == 1, calls
    assert lc._POOLSIDE_MODEL in calls and lc._OPENROUTER_MODEL in calls
    assert calls.index(lc._POOLSIDE_MODEL) > calls.index(reasoner)
    assert out.attempts == (
        f"{coder} (parse_failed)",
        f"{reasoner} (ReadTimeout)",
        f"{lc._POOLSIDE_MODEL} (http_503)",
        f"{lc._OPENROUTER_MODEL} (ConnectError)",
    )

    evaluation = evaluate_diff((), out)
    title, summary = cr_dispatch._summary_markdown(evaluation, review_phase="tier1")
    assert title.startswith("WARN Grug eyes clouded (parse_failed: tried ")
    assert f"{coder} (parse_failed)" in title
    assert f"{reasoner} (ReadTimeout)" in title
    assert f"{lc._POOLSIDE_MODEL} (http_503)" in title
    assert f"Models tried: `{coder} (parse_failed)`" in summary
    assert "Files not reviewed (1): `infra/x.tf`." in summary


def test_a_retry_with_no_untried_model_makes_no_network_call() -> None:
    """When the first walk already tried every model, the retry is free
    instead of a second identical walk."""
    calls: list[str] = []

    def respond(_url, **kwargs) -> httpx.Response:
        calls.append(kwargs["json"]["model"])
        raise httpx.ConnectError("down")

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff(
            assemble_evidence([Hunk(path="src/x.py", body="@@ -1 +1 @@\n+x")]),
            installation_id=2,
        )

    assert out.kind == "all_failed"
    assert len(calls) == len(set(calls)), f"a model was asked twice: {calls}"
    assert "models tried: " in out.error


@pytest.mark.parametrize(
    "content",
    [
        'Grug sees the tablet. Here is what Grug found.\n\n{"findings": []}',
        '{"findings": []}\n\nGrug sees the cave wall. All tablets look fine.',
    ],
)
def test_a_findings_object_wrapped_in_prose_is_repaired(content: str) -> None:
    findings, _model, err = lc._parse_response(_completion(content))
    assert err == ""
    assert findings == ()


def test_the_repair_keeps_real_findings() -> None:
    body = json.dumps({"findings": [_finding("src/x.py", "real-bug")]})
    findings, _model, err = lc._parse_response(_completion(f"Grug found one.\n{body}\nDone."))
    assert err == ""
    assert [finding.rule for finding in findings] == ["real-bug"]


def test_the_repair_rejects_an_object_without_findings() -> None:
    _findings, _model, err = lc._parse_response(
        _completion('Grug would answer like {"severity": "high"} but will not.'),
    )
    assert err.startswith("llm returned non-json")


def test_the_check_summary_names_reviewed_and_unreviewed_files() -> None:
    coverage = ReviewCoverage(
        total_cohorts=2,
        completed_cohorts=1,
        failed_cohorts=(2,),
        cohort_labels=("src", "docs"),
        unwalked_paths=("docs/b.md",),
        reviewed_paths=("src/a.py",),
    )
    evaluation = CodeReviewEvaluation(
        findings=(), conclusion="neutral",
        degraded_reason="partial_review", coverage=coverage,
    )
    _title, summary = cr_dispatch._summary_markdown(evaluation, review_phase="tier1")
    assert "Files reviewed (1): `src/a.py`." in summary
    assert "Files not reviewed (1): `docs/b.md`." in summary


def test_the_title_caps_how_many_models_it_names() -> None:
    coverage = ReviewCoverage(
        total_cohorts=1, completed_cohorts=0, failed_cohorts=(1,),
        cohort_labels=("all",),
        failed_models=tuple(f"m{i} (http_429)" for i in range(6)),
    )
    evaluation = CodeReviewEvaluation(
        findings=(), conclusion="neutral",
        degraded_reason="all_failed", coverage=coverage,
    )
    title, _summary = cr_dispatch._summary_markdown(evaluation, review_phase="tier1")
    assert title == (
        "WARN Grug eyes clouded (all_failed: tried m0 (http_429), m1 (http_429), "
        "m2 (http_429), m3 (http_429) (+2 more))"
    )


def test_merge_reports_failed_models_only_for_chunks_that_failed() -> None:
    plan = lc.plan_review(
        [Hunk(path="a.py", body="+a"), Hunk(path="b.py", body="+b")],
        max_cohort_chars=8000, max_cohort_paths=1, max_cohorts=10,
    )
    responses = [
        LlmReviewResponse(kind="reviewed", backend_used=Backend.CAVE, model_name="coder"),
        LlmReviewResponse(kind="all_failed", error="x", attempts=("coder (http_503)",)),
    ]
    out = lc._merge_cohort_responses(responses, 1, None, plan)
    assert out.coverage is not None
    assert out.coverage.failed_models == ("coder (http_503)",)
    assert out.coverage.reviewed_paths == ("a.py",)
    assert out.coverage.unwalked_paths == ("b.py",)


def _capped_completion(content: str) -> httpx.Response:
    """A parseable answer the model stopped at its max_tokens cap (grug#851)."""
    return httpx.Response(
        200,
        json={
            "choices": [{
                "message": {"role": "assistant", "content": content},
                "finish_reason": "length",
            }],
            "model": "test-model-id",
        },
    )


def test_a_token_capped_two_file_cohort_is_reviewed_one_file_at_a_time() -> None:
    """The whole diff answers with one finding but hits the token cap, so the
    review used to fail as partial with no retry. Each file alone reviews
    cleanly, so the split completes the review, and the capped answer's
    finding is kept: a split never drops evidence."""
    hunks = [
        Hunk(path="src/a.py", body="@@ -1 +1 @@\n+A_CHANGE"),
        Hunk(path="src/b.py", body="@@ -1 +1 @@\n+B_CHANGE"),
    ]
    prompts: list[str] = []

    def respond(_url, **kwargs) -> httpx.Response:
        prompt = kwargs["json"]["messages"][1]["content"]
        prompts.append(prompt)
        if "A_CHANGE" in prompt and "B_CHANGE" in prompt:
            return _capped_completion(
                json.dumps({"findings": [_finding("src/a.py", "capped-bug")]}),
            )
        if "A_CHANGE" in prompt:
            return _completion(json.dumps({"findings": [_finding("src/a.py", "a-bug")]}))
        return _completion(json.dumps({"findings": []}))

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff(assemble_evidence(hunks), installation_id=2)

    assert out.kind == "reviewed", out.error
    assert out.error == ""
    assert {finding.rule for finding in out.findings} == {"capped-bug", "a-bug"}
    assert out.coverage is not None
    assert out.coverage.capped_cohorts == ()
    assert out.coverage.reviewed_paths == ("src/a.py", "src/b.py")
    single_file = [p for p in prompts if ("A_CHANGE" in p) != ("B_CHANGE" in p)]
    assert len(single_file) == 2, "each file chunk is one model call"


def test_a_capped_file_chunk_keeps_the_parent_finding_and_stays_partial() -> None:
    """A file chunk that fails after the split falls back to the capped
    answer for that file: its finding survives and the review stays partial."""
    hunks = [Hunk(path="a.py", body="+a"), Hunk(path="b.py", body="+b")]
    plan = lc.plan_review(
        hunks, max_cohort_chars=8000, max_cohort_paths=5, max_cohorts=10,
    )
    finding = lc.Finding(path="b.py", line=1, rule="b-bug", severity="medium", message="m")
    capped = LlmReviewResponse(
        kind="reviewed", findings=(finding,),
        backend_used=Backend.CAVE, model_name="coder",
        error=lc._truncation_error((Backend.CAVE,)),
    )
    chunks = {0: lc._file_chunks(plan.cohorts[0], hunks)}
    sub = [
        LlmReviewResponse(kind="reviewed", backend_used=Backend.CAVE, model_name="coder"),
        LlmReviewResponse(kind="all_failed", error="down"),
    ]
    spliced_plan, spliced = lc._splice_file_chunks(plan, [capped], chunks, sub)
    out = lc._merge_cohort_responses(spliced, 1, None, spliced_plan)
    assert [finding.rule for finding in out.findings] == ["b-bug"]
    assert out.error.startswith("partial review:")
    assert out.coverage is not None
    assert out.coverage.capped_cohorts == (2,)
    assert out.coverage.reviewed_paths == ("a.py", "b.py")


def test_a_capped_single_file_cohort_is_named_in_the_coverage_line() -> None:
    """One file cannot be split smaller, so the review stays partial, and the
    summary now says why instead of a bare "1/1 cohorts completed"."""
    plan = lc.plan_review(
        [Hunk(path="a.py", body="+a")],
        max_cohort_chars=8000, max_cohort_paths=1, max_cohorts=10,
    )
    responses = [
        LlmReviewResponse(
            kind="reviewed", backend_used=Backend.CAVE, model_name="coder",
            error=lc._truncation_error((Backend.CAVE,)),
        ),
    ]
    out = lc._merge_cohort_responses(responses, 1, None, plan)
    assert out.error.startswith("partial review:")
    evaluation = evaluate_diff((), out)
    assert evaluation.degraded_reason == "partial_review"
    _title, summary = cr_dispatch._summary_markdown(evaluation, review_phase="tier1")
    assert "Coverage: 1/1 cohorts completed; hit the token cap: 1." in summary
