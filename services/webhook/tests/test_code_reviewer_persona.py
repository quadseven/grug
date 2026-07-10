"""Tests for personas/code_reviewer/persona.py."""
from __future__ import annotations

from llm_client import (
    Backend,
    Finding as LlmFinding,
    FindingOrigin,
    LlmReviewResponse,
)
from personas.code_reviewer.diff_parser import parse_diff
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


def test_partial_response_keeps_provisional_findings_but_never_blocks() -> None:
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="partial",
        findings=(_llm_finding(rule="silent-exception", line=2),),
        backend_used=Backend.POOLSIDE,
        error="openrouter: timeout",
    )

    out = evaluate_diff(hunks, llm)

    assert out.conclusion == "neutral"
    assert out.degraded_reason == "partial"
    assert [finding.rule_name for finding in out.findings] == ["silent-exception"]


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


def test_evaluate_diff_preserves_every_wire_finding_origin() -> None:
    """A merged ensemble finding keeps all producer spans after the
    wire-to-persona translation so judge and reaction evals can fan out."""
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
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py",
            line=2,
            rule="null-deref",
            severity="high",
            message="m",
            origins=origins,
        ),),
    )

    out = evaluate_diff(parse_diff(_DIFF), llm)

    assert out.findings[0].origins == origins


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
    # Mutate a real field (not the `passed` property — that raises a
    # different error for a different reason).
    try:
        out.conclusion = "failure"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("CodeReviewEvaluation should be frozen")


def test_passed_is_derived_from_conclusion() -> None:
    """`passed` is a property — single source of truth is `conclusion`.
    No way to construct an evaluation where `passed=True` but
    `conclusion="failure"` (the prior shape allowed exactly that)."""
    success_eval = CodeReviewEvaluation(findings=(), conclusion="success")
    failure_eval = CodeReviewEvaluation(findings=(), conclusion="failure")
    neutral_eval = CodeReviewEvaluation(findings=(), conclusion="neutral")
    assert success_eval.passed is True
    assert failure_eval.passed is False
    assert neutral_eval.passed is True  # advisory-first


def test_finding_suggestion_can_be_none() -> None:
    """`suggestion: str | None` — None means LLM didn't supply a hint.
    Previously `""` served this purpose, which conflated absent with
    empty."""
    f = Finding(
        file="x.py", line=1, severity="low", rule_name="r", message="m",
        suggestion=None,
    )
    assert f.suggestion is None


def test_finding_rejects_zero_line() -> None:
    """GitHub's inline-comment API rejects line=0 with a 422; assertion
    catches at parse time, not GH POST."""
    import pytest as _pytest
    with _pytest.raises(AssertionError, match="line must be >= 1"):
        Finding(file="x.py", line=0, severity="low", rule_name="r", message="m", suggestion=None)


def test_low_severity_kept_but_advisory() -> None:
    """Low severity is reported, kept in `findings`, but doesn't flip
    `passed`. Without this, a refactor that excluded low from `kept`
    would ship green (`success` + `findings=()` looks like clean PR)."""
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(severity="low", line=2),),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(hunks, llm)
    assert out.passed is True
    assert out.conclusion == "success"
    assert len(out.findings) == 1
    assert out.findings[0].severity == "low"


def test_mixed_blocking_and_advisory_kept_together() -> None:
    """A critical + a low coexist: both retained in `findings`, but
    `conclusion=failure` driven by the critical. Catches a regression
    that drops non-blocking findings when blocking ones exist."""
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            _llm_finding(severity="critical", line=2, rule="crit"),
            _llm_finding(severity="low", line=3, rule="info"),
        ),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(hunks, llm)
    assert out.conclusion == "failure"
    assert out.passed is False
    assert len(out.findings) == 2
    assert {f.rule_name for f in out.findings} == {"crit", "info"}


def test_hallucination_wrong_file_does_not_raise() -> None:
    """A finding whose `path` is not in the line index must take the
    `allowed_lines is None` branch and increment dropped. Catches a
    regression that uses `dict[path]` (KeyError) instead of `.get()`."""
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            _llm_finding(path="totally-unseen.py", line=1, rule="ghost"),
        ),
        backend_used=Backend.POOLSIDE,
    )
    # Must not raise — if it does, `_hunk_line_index` switched to a
    # KeyError shape and the wrong-file path stopped being graceful.
    out = evaluate_diff(hunks, llm)
    assert out.findings == ()
    assert out.dropped_hallucinations == 1


def test_dropped_hallucinations_count_surfaced() -> None:
    """Two hallucinated findings + one real → dropped_hallucinations=2.
    Distinguishes "100% hallucination" from "no findings at all" — both
    yield findings=() under the old shape, only one is a real clean PR."""
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            _llm_finding(line=2),     # real
            _llm_finding(line=999),   # hallucination
            _llm_finding(path="not-in-diff.py", line=2),  # hallucination
        ),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(hunks, llm)
    assert len(out.findings) == 1
    assert out.dropped_hallucinations == 2


def test_all_failed_preserves_degraded_reason() -> None:
    """`degraded_reason` carries the LlmReviewResponse.kind so dispatch
    metrics can tell "LLM provider outage" from "empty PR"."""
    llm = LlmReviewResponse(kind="all_failed", error="poolside: timeout")
    out = evaluate_diff((), llm)
    assert out.degraded_reason == "all_failed"


def test_no_diff_preserves_degraded_reason() -> None:
    llm = LlmReviewResponse(kind="no_diff")
    out = evaluate_diff((), llm)
    assert out.degraded_reason == "no_diff"


def test_parse_failed_preserves_degraded_reason() -> None:
    llm = LlmReviewResponse(
        kind="parse_failed", error="non-json", backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff((), llm)
    assert out.degraded_reason == "parse_failed"


def test_reviewed_response_has_no_degraded_reason() -> None:
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(line=2),),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(hunks, llm)
    assert out.degraded_reason is None


def test_diff_hunk_rejects_missing_at_at_in_body() -> None:
    """Boundary check — catches a parser regression that loses the @@
    header before the body string is captured. Raises DiffParseError
    (the exception the dispatch degrade contract catches), never a bare
    AssertionError that would escape it and poison the consumer."""
    import pytest as _pytest
    from personas.code_reviewer.diff_parser import DiffHunk as _DH, DiffParseError as _DPE
    with _pytest.raises(_DPE, match="@@ hunk header"):
        _DH(file_path="x.py", new_start=1, new_lines=frozenset({1}), body="no header here")


def test_evaluate_diff_carries_suggestion_and_effort():
    """#553: the wire-format suggestion/effort survive translation into the
    persona Finding (they were hardwired None/absent before)."""
    from llm_client import Finding as WireFinding
    from llm_client import LlmReviewResponse
    from personas.code_reviewer.diff_parser import parse_diff
    from personas.code_reviewer.persona import evaluate_diff

    hunks = parse_diff(
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1 +1,2 @@\n a = 1\n+b = use(x)\n"
    )
    resp = LlmReviewResponse(
        kind="reviewed",
        findings=(
            WireFinding(
                path="x.py", line=2, rule="null-deref", severity="high",
                message="m", suggestion="b = use(x) if x else None",
                effort="quick-win",
            ),
        ),
    )
    ev = evaluate_diff(hunks, resp)
    assert ev.findings[0].suggestion == "b = use(x) if x else None"
    assert ev.findings[0].effort == "quick-win"
