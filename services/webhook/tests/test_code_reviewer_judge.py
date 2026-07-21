"""Tests for personas/code_reviewer/judge.run_judge.

The judge orchestrator runs AFTER a review is published: it grades each
surviving finding via a second LLM call and submits per-finding
`is_real_bug` evaluations to DD LLM Obs, attached to the review span.
Best-effort — a judge failure never affects the (already-published)
review."""
from __future__ import annotations

from llm_client import Backend, FindingJudgement, FindingOrigin, LlmReviewResponse
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


def _finding(
    rule="r", line=2, severity="medium", origins=(), file="src/x.py",
) -> Finding:
    return Finding(
        file=file, line=line, severity=severity, rule_name=rule,
        message="m", suggestion=None, origins=origins,
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
        lambda fr, h, installation_id, **kwargs: (
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


def test_run_judge_fans_out_eval_to_every_finding_origin(monkeypatch):
    """One deduplicated finding can be produced by both ensemble models.
    The judge verdict must train both review spans with source attribution."""
    origins = (
        FindingOrigin(
            backend=Backend.POOLSIDE,
            model="poolside/laguna-m.1",
            review_span_context={"span_id": "poolside-span"},
        ),
        FindingOrigin(
            backend=Backend.OPENROUTER,
            model="anthropic/claude-opus-4.7",
            review_span_context={"span_id": "openrouter-span"},
        ),
    )
    evaluation = CodeReviewEvaluation(
        findings=(_finding(rule="null-deref", origins=origins),),
        conclusion="success",
    )
    monkeypatch.setattr(
        cr_judge,
        "judge_findings",
        lambda *a, **kw: (FindingJudgement(0, True, "real"),),
    )
    submitted: list[dict] = []
    monkeypatch.setattr(
        cr_judge,
        "submit_finding_evaluation",
        lambda **kw: submitted.append(kw),
    )

    cr_judge.run_judge(
        evaluation,
        parse_diff(_DIFF),
        installation_id=1,
        review_span_context=None,
    )

    assert [s["review_span_context"] for s in submitted] == [
        {"span_id": "poolside-span"},
        {"span_id": "openrouter-span"},
    ]
    assert [s["tags"]["source_backend"] for s in submitted] == [
        "poolside",
        "openrouter",
    ]
    assert [s["tags"]["source_model"] for s in submitted] == [
        "poolside/laguna-m.1",
        "anthropic/claude-opus-4.7",
    ]


def test_submit_evals_does_not_misattribute_failed_origin_export(monkeypatch):
    """A Poolside finding whose trace export failed must not be attached to
    the response-level OpenRouter span."""
    finding = _finding(origins=(FindingOrigin(
        backend=Backend.POOLSIDE,
        model="poolside/laguna-m.1",
        review_span_context=None,
    ),))
    submitted: list[dict] = []
    monkeypatch.setattr(
        cr_judge,
        "submit_finding_evaluation",
        lambda **kw: submitted.append(kw),
    )

    cr_judge.submit_evals(
        (finding,),
        (FindingJudgement(0, True, "real"),),
        review_span_context={"span_id": "legacy-span"},
    )

    assert submitted == []


def test_submit_evals_uses_response_span_for_legacy_finding(monkeypatch):
    finding = _finding(origins=())
    submitted: list[dict] = []
    monkeypatch.setattr(
        cr_judge,
        "submit_finding_evaluation",
        lambda **kw: submitted.append(kw),
    )

    cr_judge.submit_evals(
        (finding,),
        (FindingJudgement(0, True, "real"),),
        review_span_context={"span_id": "legacy-span"},
    )

    assert submitted[0]["review_span_context"] == {"span_id": "legacy-span"}
    assert "source_backend" not in submitted[0]["tags"]


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
        lambda fr, h, installation_id, **kwargs: (
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
        lambda fr, h, installation_id, **kwargs: (
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


def test_grade_findings_batches_large_ensemble_output(monkeypatch):
    """A high-recall ensemble must not make the judge skip every candidate."""
    total = cr_judge.JUDGE_BATCH_SIZE + 2
    evaluation = CodeReviewEvaluation(
        findings=tuple(_finding(rule=f"rule-{i}") for i in range(total)),
        conclusion="success",
    )
    batch_sizes: list[int] = []

    def judge_batch(reprs, *args, **kwargs):
        batch_sizes.append(len(reprs))
        return tuple(
            FindingJudgement(i, True, "real") for i in range(len(reprs))
        )

    monkeypatch.setattr(cr_judge, "judge_findings", judge_batch)

    verdicts = cr_judge.grade_findings(
        evaluation, parse_diff(_DIFF), installation_id=1,
    )

    assert batch_sizes == [cr_judge.JUDGE_BATCH_SIZE, 2]
    assert [verdict.finding_index for verdict in verdicts] == list(range(total))


def test_grade_findings_caps_total_batches_and_leaves_remainder_ungraded(
    monkeypatch, caplog,
):
    total = cr_judge.JUDGE_MAX_FINDINGS + 2
    evaluation = CodeReviewEvaluation(
        findings=tuple(_finding(rule=f"rule-{i}") for i in range(total)),
        conclusion="success",
    )
    batch_sizes: list[int] = []

    def judge_batch(reprs, *args, **kwargs):
        batch_sizes.append(len(reprs))
        return tuple(
            FindingJudgement(i, False, "noise", confidence=1.0)
            for i in range(len(reprs))
        )

    monkeypatch.setattr(cr_judge, "judge_findings", judge_batch)

    verdicts = cr_judge.grade_findings(
        evaluation, parse_diff(_DIFF), installation_id=1,
    )
    kept, suppressed = cr_judge.partition_findings(
        evaluation.findings, verdicts,
    )

    assert batch_sizes == [cr_judge.JUDGE_BATCH_SIZE] * 3
    assert len(verdicts) == cr_judge.JUDGE_MAX_FINDINGS
    assert len(suppressed) == cr_judge.JUDGE_MAX_FINDINGS
    assert [finding.rule_name for finding in kept] == [
        f"rule-{cr_judge.JUDGE_MAX_FINDINGS}",
        f"rule-{cr_judge.JUDGE_MAX_FINDINGS + 1}",
    ]
    assert any(record.message == "judge_total_cap_reached" for record in caplog.records)


def test_grade_findings_scopes_review_evidence_to_candidate_files(monkeypatch):
    diff = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1 +1 @@
-old
+new
diff --git a/tests/test_y.py b/tests/test_y.py
--- a/tests/test_y.py
+++ b/tests/test_y.py
@@ -1 +1 @@
-old_test
+new_test
"""
    evaluation = CodeReviewEvaluation(
        findings=(_finding(rule="caller-not-updated"),), conclusion="success",
    )
    captured: dict = {}

    def judge_batch(reprs, hunks, *args, **kwargs):
        captured["hunks"] = hunks
        captured.update(kwargs)
        return (FindingJudgement(0, True, "real"),)

    monkeypatch.setattr(cr_judge, "judge_findings", judge_batch)

    verdicts = cr_judge.grade_findings(
        evaluation,
        parse_diff(diff),
        installation_id=1,
        pr_context={"title": "contract"},
        file_contents={
            "src/x.py": "changed",
            "tests/test_y.py": "unrelated",
        },
        cross_file_contents={"src/caller.py": "old_call()"},
        runtime_context="10 errors",
    )

    assert len(verdicts) == 1
    assert [h.path for h in captured["hunks"]] == ["src/x.py"]
    assert captured["file_contents"] == {"src/x.py": "changed"}
    assert captured["cross_file_contents"] == {"src/caller.py": "old_call()"}
    assert captured["runtime_context"] == "10 errors"
    assert captured["redact"] is True


def test_grade_findings_uses_owned_reasoner_then_redacted_fallback(monkeypatch):
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://gateway")
    monkeypatch.delenv("GRUG_CAVE_JUDGE_MODEL", raising=False)
    evaluation = CodeReviewEvaluation(
        findings=(_finding(rule="caller-not-updated"),), conclusion="success",
    )
    calls: list[dict] = []

    def judge_batch(reprs, hunks, *args, **kwargs):
        calls.append(kwargs)
        if kwargs["config"] is not None:
            return ()
        return (FindingJudgement(0, True, "confirmed"),)

    monkeypatch.setattr(cr_judge, "judge_findings", judge_batch)

    verdicts = cr_judge.grade_findings(
        evaluation, parse_diff(_DIFF), installation_id=1,
    )

    assert len(verdicts) == 1
    assert calls[0]["config"].model == "poolside/Laguna-S-2.1-NVFP4"
    assert calls[0]["redact"] is False
    assert calls[1]["config"] is None
    assert calls[1]["redact"] is True


def test_grade_findings_falls_back_when_owned_reasoner_is_partial(monkeypatch):
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://gateway")
    evaluation = CodeReviewEvaluation(
        findings=(_finding(rule="one"), _finding(rule="two", line=3)),
        conclusion="success",
    )
    calls: list[str] = []

    def judge_batch(reprs, hunks, *args, **kwargs):
        owned = kwargs["config"] is not None
        calls.append("owned" if owned else "fallback")
        if owned:
            return (FindingJudgement(0, True, "only one"),)
        return tuple(
            FindingJudgement(i, True, "complete") for i in range(len(reprs))
        )

    monkeypatch.setattr(cr_judge, "judge_findings", judge_batch)

    verdicts = cr_judge.grade_findings(
        evaluation, parse_diff(_DIFF), installation_id=1,
    )

    assert calls == ["owned", "fallback"]
    assert len(verdicts) == 2


def test_refute_findings_scopes_review_evidence_to_candidate_files(monkeypatch):
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL", raising=False)
    diff = _DIFF + """diff --git a/src/y.py b/src/y.py
--- a/src/y.py
+++ b/src/y.py
@@ -1 +1 @@
-old_y
+new_y
"""
    captured: dict = {}

    def judge_batch(reprs, hunks, *args, **kwargs):
        captured["hunks"] = hunks
        captured.update(kwargs)
        return (FindingJudgement(0, True, "confirmed"),)

    monkeypatch.setattr(cr_judge, "judge_findings", judge_batch)

    verdicts = cr_judge.refute_findings(
        (_finding(severity="high"),),
        parse_diff(diff),
        installation_id=1,
        file_contents={"src/x.py": "changed", "src/y.py": "unrelated"},
    )

    assert len(verdicts) == 1
    assert [h.path for h in captured["hunks"]] == ["src/x.py"]
    assert captured["file_contents"] == {"src/x.py": "changed"}
    assert captured["refute"] is True
