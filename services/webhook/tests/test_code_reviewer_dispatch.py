"""Tests for personas/code_reviewer/dispatch.dispatch_code_review.

The dispatch function orchestrates: fetch PR diff via GH API → parse →
LLM → evaluate → publish check-run + inline review. Mocks every
downstream call; no real network or DDB."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse
from personas.code_reviewer import dispatch as cr_dispatch


_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
"""


def _payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "installation": {"id": 11},
        "repository": {
            "id": 22,
            "name": "myrepo",
            "owner": {"login": "myorg"},
        },
        "pull_request": {
            "number": 7,
            "head": {"sha": "abcd1234efgh"},
        },
    }


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch):
    """Skip the real with_install_token_retry by stubbing it to call
    the wrapped function directly with a fake token."""
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("fake-token"),
    )


def _diff_response(diff: str = _DIFF):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = diff
    return r


def test_dispatch_advisory_mode_posts_neutral_check_and_comment_review(monkeypatch):
    """Default mode (`code_reviewer_blocking=False`): check-run
    conclusion=neutral, review event=COMMENT. Both clients called with
    the right payload."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity="medium", message="catches Exception silently",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        model_name="laguna",
    )
    posted_check = []
    posted_review = []

    def _fake_review_diff(hunks, installation_id, pr_context=None):
        return llm

    def _fake_post_check_run(install_token, owner, repo, result, external_id=None):
        posted_check.append({"owner": owner, "repo": repo, "result": result})
        return {"id": 1}

    def _fake_post_review(install_token, owner, repo, *, pull_number, result):
        posted_review.append({"pull_number": pull_number, "result": result})
        return {"id": 2}

    monkeypatch.setattr(cr_dispatch, "review_diff", _fake_review_diff)
    monkeypatch.setattr(cr_dispatch, "post_check_run", _fake_post_check_run)
    monkeypatch.setattr(cr_dispatch, "post_review", _fake_post_review)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(
            _payload(), blocking=False,
        )

    assert out == {"persona": "code_reviewer", "result": "pass"}
    assert len(posted_check) == 1
    assert posted_check[0]["result"].conclusion == "neutral"  # advisory
    assert len(posted_review) == 1
    assert posted_review[0]["result"].event == "COMMENT"  # advisory
    # Inline comments include the finding.
    inline = posted_review[0]["result"].comments
    assert len(inline) == 1
    assert inline[0].path == "src/x.py"
    assert inline[0].line == 2


def test_dispatch_blocking_mode_uses_failure_conclusion_and_request_changes(monkeypatch):
    """`code_reviewer_blocking=True` + a high/critical finding flips
    check-run conclusion=failure and review event=REQUEST_CHANGES."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="secret-leak",
            severity="critical", message="API key in log",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )
    posted_check, posted_review = [], []

    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    assert posted_check[0].conclusion == "failure"
    assert posted_review[0].event == "REQUEST_CHANGES"


def test_dispatch_no_findings_posts_clean_pass(monkeypatch):
    """LLM returned reviewed + empty findings → check conclusion=success
    (or neutral in advisory mode), review event=COMMENT, zero inline
    comments. Don't post a noisy "looks good!" inline comment when
    there's nothing to flag."""
    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
    )
    posted_check, posted_review = [], []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    # Clean pass — check posts but with no findings table.
    assert posted_check[0].conclusion == "neutral"  # advisory always neutral
    # No inline review when there's nothing to comment on.
    assert posted_review == []


def test_dispatch_llm_outage_does_not_block_pr(monkeypatch):
    """LLM all_failed → advisory check-run posted with conclusion=neutral
    and degraded_reason message. No inline review (nothing to comment
    on). Elder must not 500 the dispatcher on LLM outages."""
    llm = LlmReviewResponse(kind="all_failed", error="poolside: timeout")
    posted_check, posted_review = [], []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    # Even in blocking mode, an LLM outage degrades to neutral —
    # advisory-first contract.
    assert posted_check[0].conclusion == "neutral"
    assert posted_review == []
    assert out == {"persona": "code_reviewer", "result": "skipped"}


def test_dispatch_unparseable_diff_yields_neutral(monkeypatch):
    """parse_diff raises DiffParseError on a malformed @@ header. Caller
    must catch and degrade to advisory neutral — Elder cannot fail a
    PR on parser issues."""
    bad_diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ garbled @@\n+foo\n"
    posted_check = []
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )

    with patch("httpx.get", return_value=_diff_response(bad_diff)):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert posted_check[0].conclusion == "neutral"
    assert out["result"] == "skipped"


def test_dispatch_review_publish_failure_returns_publish_failed(monkeypatch):
    """Inline-review publish 5xx must surface as `publish_failed`, not
    silently `pass`. Without this, DD dashboards would overstate
    success — inline comments never reached GitHub yet the log fires
    with result=pass and findings_count=N."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="x", severity="medium", message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})

    def _raise(*a, **kw):
        raise httpx.ConnectError("reviews API down")

    monkeypatch.setattr(cr_dispatch, "post_review", _raise)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "publish_failed"


def test_dispatch_check_run_publish_failure_returns_publish_failed(monkeypatch):
    """Check-run is the load-bearing GH surface (flips mergeability in
    blocking mode). If it fails to publish, the persona result must be
    `publish_failed` not `pass`/`fail` — operator needs to see the
    distinct state to triage."""
    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    def _raise(*a, **kw):
        raise httpx.ConnectError("checks API down")

    monkeypatch.setattr(cr_dispatch, "post_check_run", _raise)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "publish_failed"


def test_dispatch_llm_degraded_logs_warning(monkeypatch, caplog):
    """A 100% LLM-outage rate must produce a distinct log signal so
    DD/dashboards can monitor backend health per-install. Without
    this, all_failed looks identical to "no findings" in logs."""
    llm = LlmReviewResponse(
        kind="all_failed", error="poolside: timeout",
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with caplog.at_level("WARNING"):
        with patch("httpx.get", return_value=_diff_response()):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert any(
        "code_review_llm_degraded" in r.message for r in caplog.records
    )


def test_dispatch_degraded_publish_failure_returns_publish_failed(monkeypatch):
    """Fetch fails AND the degraded check-run publish also fails →
    result must be `publish_failed`, not `skipped`. Without this, a
    regression that silently swallows the degraded publish would mask
    a "no check-run at all" production state as a benign "skipped"."""
    # Make the diff fetch raise so we enter the _publish_degraded path.
    def _fetch_raises(*a, **kw):
        raise httpx.ConnectError("github down")

    def _post_raises(*a, **kw):
        raise httpx.ConnectError("checks API also down")

    monkeypatch.setattr(cr_dispatch, "post_check_run", _post_raises)

    with patch("httpx.get", side_effect=_fetch_raises):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "publish_failed"


def test_blocking_mode_failure_produces_request_changes(monkeypatch):
    """The ONLY path that actually blocks a merge:
    mode=blocking + evaluation.conclusion=failure →
    (check.conclusion=failure, review.event=REQUEST_CHANGES).
    Advisory + degraded tests cover the other branches; this is the
    one that makes blocking-mode mean something."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="critical-rule",
            severity="critical", message="secret leak",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )
    posted_check, posted_review = [], []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    assert posted_check[0].conclusion == "failure"
    assert posted_review[0].event == "REQUEST_CHANGES"
    assert out["result"] == "fail"


def test_resolve_result_publish_failed_wins_over_skipped():
    """When check-run publish fails AND the evaluation is degraded
    (e.g. all_failed LLM), publish_failed must win. Operator needs to
    see the publish failure (production-visible) over the skip reason
    (already implied by neutral conclusion)."""
    from personas.code_reviewer.persona import CodeReviewEvaluation
    degraded = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="all_failed",
    )
    assert cr_dispatch._resolve_result(
        degraded, check_publish_failed=True,
    ) == "publish_failed"


def test_dispatch_emits_structured_log_on_success(monkeypatch, caplog):
    """Acceptance criterion (#186): "Grug webhook logs show
    `code_reviewer_dispatched` structured log entry." Operator uses
    this to confirm Elder ran on a real PR end-to-end. Must include
    pr, installation_id, backend, model, finding count, and result."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity="medium", message="catches Exception silently",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        model_name="poolside/laguna-m.1",
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with caplog.at_level("INFO"):
        with patch("httpx.get", return_value=_diff_response()):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    dispatched_records = [
        r for r in caplog.records if r.message == "code_reviewer_dispatched"
    ]
    assert len(dispatched_records) == 1
    extra = dispatched_records[0].__dict__
    # Operator must be able to filter by install + PR coords.
    assert extra.get("installation_id") == 11
    assert extra.get("pr") == "myorg/myrepo#7"
    # Backend + model attribution for DD LLM Obs / per-backend dashboards.
    assert extra.get("backend") == "poolside"
    assert extra.get("model") == "poolside/laguna-m.1"
    # Finding count + result for at-a-glance triage.
    assert extra.get("findings_count") == 1
    assert extra.get("result") == "pass"


def test_structured_log_handles_none_backend_without_attributeerror(monkeypatch, caplog):
    """The conditional `backend_used.value if not None else None`
    guards against an AttributeError on degraded responses where
    `backend_used is None` (e.g. no_diff). This is the exact path
    operators care about monitoring — a NoneType crash here would
    silently break the degraded-backend log."""
    llm = LlmReviewResponse(kind="no_diff")  # backend_used defaults None
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with caplog.at_level("INFO"):
        with patch("httpx.get", return_value=_diff_response()):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    rec = next(
        r for r in caplog.records if r.message == "code_reviewer_dispatched"
    )
    # backend is None when no LLM call ran.
    assert rec.__dict__.get("backend") is None


def test_structured_log_carries_dropped_hallucinations_count(monkeypatch, caplog):
    """The `dropped_hallucinations` field on the log lets DD slice the
    LLM hallucination rate per backend. A regression renaming the
    attribute or substituting `len(llm_response.findings)` would
    silently break that observability slice."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            LlmFinding(path="src/x.py", line=2, rule="real", severity="medium", message="m"),  # type: ignore[arg-type]
            # Two hallucinations the filter will drop:
            LlmFinding(path="src/x.py", line=9999, rule="ghost1", severity="low", message="m"),  # type: ignore[arg-type]
            LlmFinding(path="absent.py", line=2, rule="ghost2", severity="low", message="m"),  # type: ignore[arg-type]
        ),
        backend_used=Backend.POOLSIDE,
        model_name="laguna",
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with caplog.at_level("INFO"):
        with patch("httpx.get", return_value=_diff_response()):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    rec = next(
        r for r in caplog.records if r.message == "code_reviewer_dispatched"
    )
    assert rec.__dict__.get("dropped_hallucinations") == 2
    # findings_count is the POST-drop kept count, not the raw LLM count.
    assert rec.__dict__.get("findings_count") == 1


def test_resolve_result_both_publishes_failed_returns_publish_failed():
    """Both publish failures coalesce to a single `publish_failed` (not
    double-counted, no separate `both_failed` state). Also covers the
    `review_publish_failed=True` + `evaluation.passed=False` path —
    `fail` must NOT mask the publish-failure signal."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    # Build an evaluation where evaluation.passed=False (a critical).
    failing_eval = CodeReviewEvaluation(
        findings=(Finding(
            file="x.py", line=1, severity="critical", rule_name="c",
            message="m", suggestion=None,
        ),),
        conclusion="failure",
    )
    out = cr_dispatch._resolve_result(
        failing_eval,
        check_publish_failed=True,
        review_publish_failed=True,
    )
    assert out == "publish_failed"
    # Single-publish-failure ALSO returns publish_failed (not "fail")
    # even when verdict is failure.
    out_review_only = cr_dispatch._resolve_result(
        failing_eval,
        check_publish_failed=False,
        review_publish_failed=True,
    )
    assert out_review_only == "publish_failed"


def test_dispatch_structured_log_carries_degraded_reason(monkeypatch, caplog):
    """When LLM all_failed → degraded_reason on the log so DD can
    correlate dispatch volume with backend health."""
    llm = LlmReviewResponse(kind="all_failed", error="poolside: timeout")
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with caplog.at_level("INFO"):
        with patch("httpx.get", return_value=_diff_response()):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    rec = next(
        r for r in caplog.records if r.message == "code_reviewer_dispatched"
    )
    assert rec.__dict__.get("degraded_reason") == "all_failed"
    assert rec.__dict__.get("result") == "skipped"


def test_dispatch_runs_judge_after_publish_with_review_span(monkeypatch):
    """When the review carries a span context, the LLM-as-a-judge is
    invoked AFTER publishing — with the evaluation, hunks, install id,
    and the review's span context for eval attribution."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="r", severity="medium", message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        review_span_context={"span_id": "rs1"},
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    judge_calls: list[dict] = []
    monkeypatch.setattr(
        cr_dispatch, "run_judge",
        lambda evaluation, hunks, **kw: judge_calls.append(kw),
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert len(judge_calls) == 1
    assert judge_calls[0]["review_span_context"] == {"span_id": "rs1"}
    assert judge_calls[0]["pr_context"]["repo"] == "myorg/myrepo"


def test_dispatch_judge_failure_does_not_change_result(monkeypatch):
    """The judge is pure observability — even if run_judge raises (past
    its own internal guard), the dispatch result must stand."""
    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
        review_span_context={"span_id": "rs1"},
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    def _boom(*a, **kw):
        raise RuntimeError("judge exploded past its guard")

    monkeypatch.setattr(cr_dispatch, "run_judge", _boom)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    # No findings → clean pass; judge explosion must not change it.
    assert out["result"] == "pass"


def test_dispatch_passes_pr_context_to_review_diff(monkeypatch):
    """The PR coords flow into review_diff(pr_context=...) so DD LLM
    Obs spans carry tags that filter by repo / PR / install. Without
    this, all traces would look identical in the LLM Obs UI."""
    captured = []

    def _fake_review_diff(hunks, installation_id, pr_context=None):
        captured.append(pr_context)
        return LlmReviewResponse(kind="no_diff")

    monkeypatch.setattr(cr_dispatch, "review_diff", _fake_review_diff)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert len(captured) == 1
    ctx = captured[0]
    assert ctx == {
        "installation_id": 11,
        "repo": "myorg/myrepo",
        "pr_number": 7,
        "head_sha": "abcd1234efgh",
    }


def test_dispatch_fetches_diff_with_diff_accept_header(monkeypatch):
    """Confirms the GH API call uses `Accept: application/vnd.github.diff`
    so we get the unified-diff body rather than the JSON metadata."""
    captured = []
    monkeypatch.setattr(
        cr_dispatch, "review_diff",
        lambda *a, **kw: LlmReviewResponse(kind="no_diff"),
    )
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    def capture_get(url, *, headers, timeout):
        captured.append({"url": url, "headers": headers, "timeout": timeout})
        return _diff_response()

    with patch("httpx.get", side_effect=capture_get):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert len(captured) == 1
    assert captured[0]["headers"]["Accept"] == "application/vnd.github.diff"
    assert "myorg/myrepo" in captured[0]["url"]
    assert "/pulls/7" in captured[0]["url"]
    assert captured[0]["timeout"] == 30
