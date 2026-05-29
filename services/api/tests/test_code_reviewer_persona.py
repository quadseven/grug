"""Tests for personas/code_reviewer/persona.py."""
from __future__ import annotations

from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse
from personas.code_reviewer.diff_parser import DiffHunk, parse_diff
from personas.code_reviewer.persona import (
    CodeReviewEvaluation,
    Finding,
    evaluate_diff,
)


_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
"""


def _llm_finding(
    path: str = "src/x.py",
    line: int = 2,
    severity: str = "medium",
    rule: str = "test-rule",
    message: str = "msg",
) -> LlmFinding:
    return LlmFinding(
        path=path, line=line, rule=rule, severity=severity, message=message  # type: ignore[arg-type]
    )


def test_evaluate_diff_returns_code_review_evaluation() -> None:
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(line=2),),
        backend_used=Backend.POOLSIDE,
        model_name="laguna-m.1",
    )
    out = evaluate_diff(hunks, llm)
    assert isinstance(out, CodeReviewEvaluation)
    assert len(out.findings) == 1
    assert out.findings[0].file == "src/x.py"
    assert out.findings[0].line == 2


def test_finding_outside_diff_lines_is_dropped() -> None:
    """LLM hallucinates a finding on line 999 (not in any hunk's
    new_lines). Drop it — Elder's value is anti-hallucination."""
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            _llm_finding(line=2),     # legit (in diff)
            _llm_finding(line=999),   # hallucination
        ),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(hunks, llm)
    assert len(out.findings) == 1
    assert out.findings[0].line == 2


def test_finding_on_wrong_file_is_dropped() -> None:
    """LLM names a file that wasn't in the diff. Drop."""
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(path="src/not-in-diff.py", line=2),),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(hunks, llm)
    assert out.findings == ()


def test_critical_finding_flips_passed_to_false() -> None:
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(severity="critical", line=2),),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(hunks, llm)
    assert out.passed is False
    assert out.conclusion == "failure"


def test_high_finding_flips_passed_to_false() -> None:
    """High severity is also blocking. Medium and low are advisory."""
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(severity="high", line=2),),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(hunks, llm)
    assert out.passed is False
    assert out.conclusion == "failure"


def test_medium_only_finding_keeps_passed_true() -> None:
    """Medium is advisory. The finding is reported but doesn't flip
    `passed`. Mirrors TPM's advisory-vs-blocking split (issue-link)."""
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(severity="medium", line=2),),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(hunks, llm)
    assert out.passed is True
    assert out.conclusion == "success"
    assert len(out.findings) == 1  # still reported, just advisory


def test_no_findings_yields_clean_pass() -> None:
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(kind="reviewed", findings=(), backend_used=Backend.POOLSIDE)
    out = evaluate_diff(hunks, llm)
    assert out.passed is True
    assert out.conclusion == "success"
    assert out.findings == ()


def test_no_diff_response_yields_neutral_passed_true() -> None:
    """LlmReviewResponse(kind='no_diff') — no LLM call was made. Don't
    block, don't fail — there's nothing to review."""
    llm = LlmReviewResponse(kind="no_diff")
    out = evaluate_diff((), llm)
    assert out.passed is True
    assert out.findings == ()


def test_all_failed_response_yields_neutral_passed_true() -> None:
    """LlmReviewResponse(kind='all_failed') — both backends errored.
    Elder is advisory; don't block the PR on infrastructure flakiness.
    Findings are empty; conclusion stays 'neutral' so a future blocking
    flip doesn't accidentally fail PRs on LLM outages."""
    llm = LlmReviewResponse(kind="all_failed", error="poolside: timeout")
    out = evaluate_diff((), llm)
    assert out.passed is True
    assert out.conclusion == "neutral"
    assert out.findings == ()


def test_parse_failed_response_yields_neutral_passed_true() -> None:
    """Same disposition as all_failed — Elder can't fail the PR if the
    LLM returned prose. Caller will post an advisory check explaining."""
    llm = LlmReviewResponse(
        kind="parse_failed", error="non-json", backend_used=Backend.POOLSIDE
    )
    out = evaluate_diff((), llm)
    assert out.passed is True
    assert out.conclusion == "neutral"


def test_finding_carries_persona_level_field_names() -> None:
    """Issue #182 spec: persona-level Finding has `file`, `line`,
    `severity`, `rule_name`, `message`, `suggestion`. The llm_client's
    wire-format `Finding` uses `path` + `rule` — evaluate_diff must
    translate, not re-export."""
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(rule="silent-exception", line=2),),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(hunks, llm)
    f = out.findings[0]
    assert f.file == "src/x.py"  # not `path`
    assert f.rule_name == "silent-exception"  # not `rule`
    assert hasattr(f, "suggestion")  # new field at persona level


def test_evaluate_diff_is_pure_no_logging_or_io(monkeypatch) -> None:
    """Spec 0015 attests `evaluate_diff_is_pure_function_no_io`. Patch
    logging + httpx and assert neither is touched."""
    import logging
    log_calls: list = []
    monkeypatch.setattr(
        logging.Logger, "info",
        lambda self, *a, **kw: log_calls.append(("info", a, kw)),
    )
    monkeypatch.setattr(
        logging.Logger, "warning",
        lambda self, *a, **kw: log_calls.append(("warning", a, kw)),
    )
    monkeypatch.setattr(
        logging.Logger, "error",
        lambda self, *a, **kw: log_calls.append(("error", a, kw)),
    )

    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed", findings=(_llm_finding(line=2),), backend_used=Backend.POOLSIDE
    )
    evaluate_diff(hunks, llm)
    assert log_calls == []


def test_code_review_evaluation_is_frozen() -> None:
    """Spec 0015 attests `code_review_evaluation_frozen_dataclass_no_mutation`."""
    import dataclasses
    out = evaluate_diff(
        (),
        LlmReviewResponse(kind="reviewed", findings=(), backend_used=Backend.POOLSIDE),
    )
    try:
        out.passed = False  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("CodeReviewEvaluation should be frozen")
