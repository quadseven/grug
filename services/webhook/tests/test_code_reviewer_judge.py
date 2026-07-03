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


def test_run_judge_redacts_exposed_secret_reasoning(monkeypatch):
    """#436: an exposed-secret finding's judge reasoning is generated from full
    raw file context and can quote the credential; it must be redacted before
    the DD eval submit. A normal finding's reasoning passes through unchanged."""
    from personas.code_reviewer.sast import EXPOSED_SECRET

    evaluation = CodeReviewEvaluation(
        findings=(_finding(rule=EXPOSED_SECRET), _finding(rule="null-deref", line=3)),
        conclusion="failure",
    )
    review = _reviewed(evaluation.findings)
    monkeypatch.setattr(
        cr_judge, "judge_findings",
        lambda fr, h, installation_id, pr_context=None, file_contents=None: (
            FindingJudgement(0, True, "the key AKIAIOSFODNN7EXAMPLE is live and reaches prod"),
            FindingJudgement(1, True, "real null deref"),
        ),
    )
    submitted: list[dict] = []
    monkeypatch.setattr(cr_judge, "submit_finding_evaluation", lambda **kw: submitted.append(kw))

    cr_judge.run_judge(
        evaluation, parse_diff(_DIFF), installation_id=1,
        review_span_context=review.review_span_context,
        file_contents={"src/x.py": "AKIAIOSFODNN7EXAMPLE"},
    )

    assert submitted[0]["tags"]["rule_name"] == EXPOSED_SECRET
    assert "AKIAIOSFODNN7EXAMPLE" not in submitted[0]["reasoning"]
    assert submitted[0]["reasoning"] == "[redacted: exposed-secret]"
    assert submitted[1]["reasoning"] == "real null deref"  # normal finding untouched


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


# ── #467 judge-gated publication ──────────────────────────────────────


def test_partition_suppresses_confident_medium_false_positive():
    """A finding the judge marks not-real at/above the confidence floor
    with severity<=medium is suppressed; a real one is kept."""
    findings = (
        _finding(rule="real-bug", severity="medium"),
        _finding(rule="style-nit", severity="medium", line=3),
    )
    verdicts = (
        FindingJudgement(0, True, "real", 0.9),
        FindingJudgement(1, False, "nit", 0.9),
    )
    kept, suppressed = cr_judge.partition_findings(
        findings, verdicts, confidence_floor=0.7,
    )
    assert [f.rule_name for f in kept] == ["real-bug"]
    assert [f.rule_name for f in suppressed] == ["style-nit"]


def test_partition_never_suppresses_high_or_critical():
    """HIGH/CRITICAL always publish even when the judge is confident it's
    a false positive (a judge FP on a critical must never hide it)."""
    findings = (
        _finding(rule="hi", severity="high"),
        _finding(rule="crit", severity="critical", line=3),
    )
    verdicts = (
        FindingJudgement(0, False, "fp", 0.99),
        FindingJudgement(1, False, "fp", 0.99),
    )
    kept, suppressed = cr_judge.partition_findings(
        findings, verdicts, confidence_floor=0.7,
    )
    assert len(kept) == 2 and suppressed == ()


def test_partition_keeps_low_confidence_false_positive():
    """A not-real verdict BELOW the confidence floor does not suppress
    (fail toward publishing when the judge is unsure)."""
    findings = (_finding(rule="maybe", severity="medium"),)
    verdicts = (FindingJudgement(0, False, "unsure", 0.5),)
    kept, suppressed = cr_judge.partition_findings(
        findings, verdicts, confidence_floor=0.7,
    )
    assert len(kept) == 1 and suppressed == ()


def test_partition_keeps_ungraded_findings_fail_open():
    """A finding with no verdict (judge outage / hallucinated index /
    over budget) is kept — fail-open."""
    findings = (_finding(rule="a"), _finding(rule="b", line=3))
    verdicts = (FindingJudgement(0, False, "fp", 0.9),)  # only finding 0 graded
    kept, suppressed = cr_judge.partition_findings(
        findings, verdicts, confidence_floor=0.7,
    )
    assert [f.rule_name for f in kept] == ["b"]
    assert [f.rule_name for f in suppressed] == ["a"]


def test_partition_empty_verdicts_keeps_everything():
    """Judge returned nothing (outage) -> zero suppression."""
    findings = (_finding(rule="a", severity="medium"),)
    kept, suppressed = cr_judge.partition_findings(
        findings, (), confidence_floor=0.7,
    )
    assert len(kept) == 1 and suppressed == ()


def test_grade_findings_fail_open_on_llm_error(monkeypatch):
    """grade_findings returns () when the judge LLM raises — the caller
    then suppresses nothing."""
    evaluation = CodeReviewEvaluation(
        findings=(_finding(),), conclusion="success",
    )

    def _boom(*a, **kw):
        raise RuntimeError("judge LLM down")

    monkeypatch.setattr(cr_judge, "judge_findings", _boom)
    verdicts = cr_judge.grade_findings(
        evaluation, parse_diff(_DIFF), installation_id=1,
    )
    assert verdicts == ()


def test_grade_findings_empty_when_no_findings():
    evaluation = CodeReviewEvaluation(findings=(), conclusion="success")
    assert cr_judge.grade_findings(
        evaluation, parse_diff(_DIFF), installation_id=1,
    ) == ()
