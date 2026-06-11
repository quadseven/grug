"""Tests for personas/code_reviewer/judge.run_judge.

The judge orchestrator runs AFTER a review is published: it grades each
surviving finding via a second LLM call and submits per-finding
`is_real_bug` evaluations to DD LLM Obs, attached to the review span.
Best-effort — a judge failure never affects the (already-published)
review."""
from __future__ import annotations

from unittest.mock import patch

from llm_client import Backend, FindingJudgement, LlmReviewResponse
from personas.code_reviewer import judge as cr_judge
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding


_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
"""


def _finding(rule="r", line=2, severity="medium") -> Finding:
    return Finding(
        file="src/x.py", line=line, severity=severity, rule_name=rule,
        message="m", suggestion=None,
    )


def _reviewed(findings, span_ctx={"span_id": "s1"}) -> LlmReviewResponse:
    return LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
        review_span_context=span_ctx,
    )


def test_run_judge_submits_one_eval_per_finding(monkeypatch):
    """Each finding gets graded and an is_real_bug eval submitted,
    attached to the review span with the finding's identity in tags."""
    evaluation = CodeReviewEvaluation(
        findings=(_finding(rule="null-deref"), _finding(rule="style", line=3)),
        conclusion="success",
    )
    review = _reviewed(evaluation.findings)

    monkeypatch.setattr(
        cr_judge, "judge_findings",
        lambda fr, h, installation_id, pr_context=None, file_contents=None: (
            FindingJudgement(0, True, "real"),
            FindingJudgement(1, False, "nit"),
        ),
    )
    submitted: list[dict] = []
    monkeypatch.setattr(
        cr_judge, "submit_finding_evaluation",
        lambda **kw: submitted.append(kw),
    )

    cr_judge.run_judge(
        evaluation, parse_diff(_DIFF), installation_id=1,
        review_span_context=review.review_span_context,
        pr_context={"repo": "o/r"},
    )

    assert len(submitted) == 2
    assert submitted[0]["is_real_bug"] is True
    assert submitted[0]["tags"]["rule_name"] == "null-deref"
    assert submitted[0]["review_span_context"] == {"span_id": "s1"}
    assert submitted[1]["is_real_bug"] is False
    assert submitted[1]["tags"]["rule_name"] == "style"


def test_run_judge_converts_diffhunks_to_wire_hunks(monkeypatch):
    """Regression: run_judge must convert parser DiffHunks → wire `Hunk`s
    (field `path`) before judge_findings, which reads `.path`. Passing raw
    DiffHunks (field `file_path`) crashed the judge with AttributeError on
    EVERY review with findings — silently killing all is_real_bug LLM-Obs
    evals — while the fully-mocked judge_findings tests stayed green."""
    evaluation = CodeReviewEvaluation(findings=(_finding(),), conclusion="success")
    review = _reviewed(evaluation.findings)
    captured: dict = {}
    monkeypatch.setattr(
        cr_judge, "judge_findings",
        lambda fr, h, installation_id, pr_context=None, file_contents=None: (
            captured.update(hunks=list(h)) or (FindingJudgement(0, True, "real"),)
        ),
    )
    monkeypatch.setattr(cr_judge, "submit_finding_evaluation", lambda **kw: None)

    cr_judge.run_judge(
        evaluation, parse_diff(_DIFF), installation_id=1,
        review_span_context=review.review_span_context, pr_context={"repo": "o/r"},
    )

    hunks = captured.get("hunks")
    assert hunks, "judge_findings received no hunks"
    # Each must expose `.path` (the wire-Hunk contract _build_judge_messages
    # reads) — a raw DiffHunk (`.file_path`) would fail this and crash judge.
    assert all(hasattr(h, "path") for h in hunks)
    assert hunks[0].path == "src/x.py"


def test_run_judge_skips_when_no_findings(monkeypatch):
    """Clean review (no findings) → no judge call, no evals."""
    evaluation = CodeReviewEvaluation(findings=(), conclusion="success")
    called = {"judge": False}
    monkeypatch.setattr(
        cr_judge, "judge_findings",
        lambda *a, **kw: called.__setitem__("judge", True) or (),
    )
    monkeypatch.setattr(cr_judge, "submit_finding_evaluation", lambda **kw: None)

    cr_judge.run_judge(
        evaluation, parse_diff(_DIFF), installation_id=1,
        review_span_context={"span_id": "s"},
    )
    assert called["judge"] is False


def test_run_judge_skips_when_no_review_span_context(monkeypatch):
    """No review span (review degraded / export failed) → can't attach
    evals; skip the judge entirely rather than burn an LLM call whose
    result can't be recorded."""
    evaluation = CodeReviewEvaluation(
        findings=(_finding(),), conclusion="success",
    )
    called = {"judge": False}
    monkeypatch.setattr(
        cr_judge, "judge_findings",
        lambda *a, **kw: called.__setitem__("judge", True) or (),
    )
    monkeypatch.setattr(cr_judge, "submit_finding_evaluation", lambda **kw: None)

    cr_judge.run_judge(
        evaluation, parse_diff(_DIFF), installation_id=1,
        review_span_context=None,
    )
    assert called["judge"] is False


def test_run_judge_tolerates_verdict_index_gaps(monkeypatch):
    """The judge LLM might return verdicts for only a subset (or
    out-of-order indices). Only findings with a matching verdict get an
    eval; a missing verdict means no eval for that finding (not a
    crash, not a default)."""
    evaluation = CodeReviewEvaluation(
        findings=(_finding(rule="a"), _finding(rule="b", line=3)),
        conclusion="success",
    )
    monkeypatch.setattr(
        cr_judge, "judge_findings",
        # Only index 1 returned; index 0 missing.
        lambda *a, **kw: (FindingJudgement(1, True, "real"),),
    )
    submitted: list[dict] = []
    monkeypatch.setattr(
        cr_judge, "submit_finding_evaluation",
        lambda **kw: submitted.append(kw),
    )

    cr_judge.run_judge(
        evaluation, parse_diff(_DIFF), installation_id=1,
        review_span_context={"span_id": "s"},
    )
    assert len(submitted) == 1
    assert submitted[0]["tags"]["rule_name"] == "b"


def test_run_judge_ignores_out_of_range_verdict_index(monkeypatch):
    """A verdict index past the findings list must be dropped, not
    IndexError. Defends against a hallucinating judge."""
    evaluation = CodeReviewEvaluation(
        findings=(_finding(),), conclusion="success",
    )
    monkeypatch.setattr(
        cr_judge, "judge_findings",
        lambda *a, **kw: (FindingJudgement(99, True, "ghost"),),
    )
    submitted: list[dict] = []
    monkeypatch.setattr(
        cr_judge, "submit_finding_evaluation",
        lambda **kw: submitted.append(kw),
    )
    cr_judge.run_judge(
        evaluation, parse_diff(_DIFF), installation_id=1,
        review_span_context={"span_id": "s"},
    )
    assert submitted == []


def test_run_judge_dedupes_duplicate_verdict_indices(monkeypatch):
    """A misbehaving judge emitting two verdicts for the same finding
    index must submit only ONE eval (first wins) — duplicate evals
    would skew the ground-truth dataset toward that finding."""
    evaluation = CodeReviewEvaluation(
        findings=(_finding(rule="a"), _finding(rule="b", line=3)),
        conclusion="success",
    )
    monkeypatch.setattr(
        cr_judge, "judge_findings",
        lambda *a, **kw: (
            FindingJudgement(0, True, "first"),
            FindingJudgement(0, False, "dup"),   # duplicate index 0
            FindingJudgement(1, True, "ok"),
        ),
    )
    submitted: list[dict] = []
    monkeypatch.setattr(
        cr_judge, "submit_finding_evaluation",
        lambda **kw: submitted.append(kw),
    )
    cr_judge.run_judge(
        evaluation, parse_diff(_DIFF), installation_id=1,
        review_span_context={"span_id": "s"},
    )
    # 2 evals (one per finding index), not 3 — the duplicate index-0
    # verdict is dropped; first wins (is_real_bug=True).
    assert len(submitted) == 2
    idx0 = [s for s in submitted if s["tags"]["rule_name"] == "a"]
    assert len(idx0) == 1
    assert idx0[0]["is_real_bug"] is True


def test_run_judge_never_raises_on_submit_failure(monkeypatch):
    """A DD submit failure must not propagate — the judge is best-effort
    and the review is already published."""
    evaluation = CodeReviewEvaluation(
        findings=(_finding(),), conclusion="success",
    )
    monkeypatch.setattr(
        cr_judge, "judge_findings",
        lambda *a, **kw: (FindingJudgement(0, True, "real"),),
    )

    def _boom(**kw):
        raise RuntimeError("DD submit exploded")

    monkeypatch.setattr(cr_judge, "submit_finding_evaluation", _boom)

    # Must not raise.
    cr_judge.run_judge(
        evaluation, parse_diff(_DIFF), installation_id=1,
        review_span_context={"span_id": "s"},
    )
