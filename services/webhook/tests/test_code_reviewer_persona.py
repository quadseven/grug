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
    with_degradation,
)
from review_pipeline import ReviewCoverage


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



def test_explicit_cross_file_finding_is_advisory_and_requires_snippet_line() -> None:
    """An explicitly linked sibling line may be reported from bounded context."""
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            LlmFinding(
                path="src/sibling.py", line=42, rule="caller-not-updated",
                severity="critical", message="Old call shape remains.",
                outside_diff_reason="The changed parse_result contract also applies here.",
            ),
            LlmFinding(
                path="src/sibling.py", line=43, rule="caller-not-updated",
                severity="high", message="This line was not supplied.",
                outside_diff_reason="The changed parse_result contract also applies here.",
            ),
        ),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(
        hunks, llm,
        cross_file_contents={"src/sibling.py": "40: def caller():\n41:     pass\n42:     parse_result()\n"},
    )
    assert len(out.findings) == 1
    finding = out.findings[0]
    assert finding.file == "src/sibling.py"
    assert finding.line == 42
    assert finding.outside_diff_reason.startswith("The changed")
    assert finding.severity == "medium"
    assert "OUTSIDE DIFF" in finding.message
    assert out.conclusion == "success"
    assert out.dropped_hallucinations == 1


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


def test_static_manifest_scalar_is_not_external_input() -> None:
    diff = """diff --git a/values.yaml b/values.yaml
--- a/values.yaml
+++ b/values.yaml
@@ -1,1 +1,2 @@
 clusterChecksRunner:
+  node.internal.example/location: oci
"""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(
            path="values.yaml", line=2, severity="critical",
            rule="unvalidated-external-input",
            message="hard-coded selector might someday be user input",
        ),),
        backend_used=Backend.POOLSIDE,
    )

    out = evaluate_diff(parse_diff(diff), llm)

    assert out.findings == ()
    assert out.conclusion == "success"


def test_templated_manifest_value_can_still_be_external_input() -> None:
    diff = """diff --git a/values.yaml b/values.yaml
--- a/values.yaml
+++ b/values.yaml
@@ -1,1 +1,2 @@
 command:
+  target: ${USER_TARGET}
"""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(
            path="values.yaml", line=2, severity="critical",
            rule="unvalidated-external-input",
        ),),
        backend_used=Backend.POOLSIDE,
    )

    out = evaluate_diff(parse_diff(diff), llm)

    assert len(out.findings) == 1


def test_static_json_scalar_is_not_external_input() -> None:
    diff = """diff --git a/config.json b/config.json
--- a/config.json
+++ b/config.json
@@ -1,2 +1,3 @@
 {
+  "location": "oci",
 }
"""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(
            path="config.json", line=2, severity="critical",
            rule="unvalidated-external-input",
        ),),
        backend_used=Backend.POOLSIDE,
    )

    assert evaluate_diff(parse_diff(diff), llm).findings == ()


def test_templated_json_key_can_still_be_external_input() -> None:
    diff = """diff --git a/config.json b/config.json
--- a/config.json
+++ b/config.json
@@ -1,2 +1,3 @@
 {
+  "${TOKEN}": "oci",
 }
"""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(
            path="config.json", line=2, severity="critical",
            rule="unvalidated-external-input",
        ),),
        backend_used=Backend.POOLSIDE,
    )

    assert len(evaluate_diff(parse_diff(diff), llm).findings) == 1


def test_diff_annotation_does_not_shift_static_scalar_lookup() -> None:
    diff = """diff --git a/values.yaml b/values.yaml
--- a/values.yaml
+++ b/values.yaml
@@ -1,1 +1,2 @@
+first: value
\\ No newline at end of file
+target: ${USER_TARGET}
"""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(
            path="values.yaml", line=2, severity="critical",
            rule="unvalidated-external-input",
        ),),
        backend_used=Backend.POOLSIDE,
    )

    assert len(evaluate_diff(parse_diff(diff), llm).findings) == 1


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


def test_partial_review_keeps_valid_findings_and_marks_coverage_degraded() -> None:
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(line=2),),
        backend_used=Backend.POOLSIDE,
        error="partial review: cohorts [2] failed",
        coverage=ReviewCoverage(
            total_cohorts=2,
            completed_cohorts=1,
            failed_cohorts=(2,),
            cohort_labels=("src", "tests"),
        ),
    )

    out = evaluate_diff(hunks, llm)

    assert len(out.findings) == 1
    assert out.degraded_reason == "partial_review"
    assert out.coverage == llm.coverage


def test_partial_review_conclusion_is_never_success() -> None:
    """grug#813/#707 - `evaluate_diff` must not build a `CodeReviewEvaluation`
    whose `conclusion` disagrees with its own `degraded_reason`.

    Before the fix, `evaluate_diff` only checked for blocking findings and
    fell through to `conclusion="success"` whenever there weren't any - it
    never consulted the `degraded_reason` it was about to set three lines
    later. A staged review that exceeded its budget (`partial review: ...`)
    but produced only advisory (or zero) findings therefore published a
    self-contradictory evaluation: `conclusion="success"` alongside
    `degraded_reason="partial_review"`. `with_extra_findings`/`with_findings`
    always used the correct 3-way rule and said so in their own docstrings
    ("the SAME rule evaluate_diff uses") - a claim that was false until now.

    MUST fail on main (`out.conclusion == "success"`) and pass after the fix
    (`out.conclusion == "neutral"`). This is the acceptance test: a diff that
    exceeds the budget must not produce a success conclusion.
    """
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        # No findings at all - the emptiest, most "looks clean" shape, and
        # therefore the one most likely to be mistaken for a full pass.
        findings=(),
        backend_used=Backend.POOLSIDE,
        error="partial review: cohorts [2] failed",
        coverage=ReviewCoverage(
            total_cohorts=2, completed_cohorts=1, failed_cohorts=(2,),
            cohort_labels=("src", "tests"),
        ),
    )

    out = evaluate_diff(hunks, llm)

    assert out.degraded_reason == "partial_review"
    assert out.conclusion == "neutral", (
        f"conclusion={out.conclusion!r} but degraded_reason={out.degraded_reason!r} "
        "- a partial-coverage evaluation must never report success"
    )
    assert out.passed is True  # advisory, not a block - Elder still doesn't gate on this


def test_partial_review_with_blocking_finding_still_fails() -> None:
    """The 3-way rule's precedence: a blocking finding wins over a partial
    pass - `failure`, not `neutral`. Partial coverage softens a clean
    verdict; it must never soften a real blocking finding into advisory."""
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(_llm_finding(line=2, severity="critical"),),
        backend_used=Backend.POOLSIDE,
        error="partial review: cohorts [2] failed",
        coverage=ReviewCoverage(
            total_cohorts=2, completed_cohorts=1, failed_cohorts=(2,),
            cohort_labels=("src", "tests"),
        ),
    )

    out = evaluate_diff(hunks, llm)

    assert out.degraded_reason == "partial_review"
    assert out.conclusion == "failure"
    assert out.passed is False


def test_with_degradation_folds_in_a_second_passs_partial_coverage() -> None:
    """The deep-review merge bug: combining Tier-1 (clean) with the deep
    arm's OWN partial coverage must not silently drop the deep arm's
    degradation. Reproduces the fold `dispatch._run_deep_review` applies to
    `combined` after `with_extra_findings`/a bare Tier-1 evaluation."""
    tier1_clean = CodeReviewEvaluation(findings=(), conclusion="success")
    deep_partial = CodeReviewEvaluation(
        findings=(),
        conclusion="neutral",
        degraded_reason="partial_review",
        coverage=ReviewCoverage(
            total_cohorts=2, completed_cohorts=1, failed_cohorts=(2,),
            cohort_labels=("a", "b"),
        ),
    )

    combined = with_degradation(tier1_clean, deep_partial)

    assert combined.degraded_reason == "partial_review"
    assert combined.conclusion == "neutral"
    assert combined.coverage == deep_partial.coverage


def test_with_degradation_uses_the_degraded_passs_coverage_not_the_clean_ones() -> None:
    """Regression: an earlier draft kept `evaluation.coverage` whenever it
    was non-None, which - when Tier-1 ITSELF ran a staged review that
    completed cleanly (so it has its own real, complete `coverage`) - would
    report Tier-1's "3/3 cohorts completed" on the SAME evaluation whose
    `degraded_reason` names a gap that is actually in the deep arm's
    different cohort plan. `coverage` must travel with whichever pass's
    `degraded_reason` is being reported, never the other one's."""
    tier1_clean_but_staged = CodeReviewEvaluation(
        findings=(),
        conclusion="success",
        coverage=ReviewCoverage(
            total_cohorts=3, completed_cohorts=3, failed_cohorts=(),
            cohort_labels=("a", "b", "c"),
        ),
    )
    deep_partial = CodeReviewEvaluation(
        findings=(),
        conclusion="neutral",
        degraded_reason="partial_review",
        coverage=ReviewCoverage(
            total_cohorts=2, completed_cohorts=1, failed_cohorts=(2,),
            cohort_labels=("x", "y"),
        ),
    )

    combined = with_degradation(tier1_clean_but_staged, deep_partial)

    assert combined.degraded_reason == "partial_review"
    assert combined.coverage == deep_partial.coverage
    assert combined.coverage.total_cohorts == 2, (
        "coverage must be the DEGRADED pass's (deep arm's) own cohort "
        "count, not the clean pass's - showing the clean pass's numbers "
        "next to a partial_review reason misrepresents which pass has "
        "the gap"
    )


def test_with_degradation_is_a_no_op_when_neither_pass_is_degraded() -> None:
    a = CodeReviewEvaluation(findings=(), conclusion="success")
    b = CodeReviewEvaluation(findings=(), conclusion="success")
    assert with_degradation(a, b) is a


def test_with_degradation_prefers_evaluations_own_reason() -> None:
    """If `evaluation` is already degraded, its own reason wins - the
    function only BACKFILLS a missing degradation, it never overwrites one
    that is already present."""
    already_degraded = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="all_failed",
    )
    other = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="partial_review",
    )
    assert with_degradation(already_degraded, other) is already_degraded


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


def test_complexity_severity_clamp_keys_on_the_marker_the_prompt_asks_for() -> None:
    """The prompt tells the model which prefix keeps a complexity finding
    above medium; the clamp must key on that same prefix, or the rule and
    the code disagree about when `high` survives."""
    from code_review_prompt import COMPLEXITY_CORRECTNESS_MARKER, build_system_prompt

    assert COMPLEXITY_CORRECTNESS_MARKER in build_system_prompt()
    hunks = parse_diff(_DIFF)
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            _llm_finding(line=2, severity="high", rule="complexity-hotspot",
                         message="Split the new branch out."),
            _llm_finding(line=3, severity="high", rule="complexity-hotspot",
                         message=f"{COMPLEXITY_CORRECTNESS_MARKER} the new "
                         "early return skips the lock release."),
            _llm_finding(line=3, severity="high", rule="test-rule",
                         message="Other kinds keep their severity."),
        ),
        backend_used=Backend.POOLSIDE,
    )
    out = evaluate_diff(hunks, llm)
    assert [f.severity for f in out.findings] == ["medium", "high", "high"]
