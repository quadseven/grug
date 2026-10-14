"""Tests for the #1165 intent-first review behavior."""
from __future__ import annotations

import re

from llm_client import Hunk, decide_deep_escalation
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.intent import (
    build_intent_paragraph,
    detect_intent_mismatch,
    filter_docs_tests_findings,
    hunks_are_docs_tests_only,
    is_docs_tests_only,
    is_sensitive,
    should_force_full_depth,
)
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding


def _finding(rule: str, severity: str = "medium") -> Finding:
    return Finding(
        file="docs/guide.md", line=1, severity=severity,  # type: ignore[arg-type]
        rule_name=rule, message="m", suggestion=None,
    )


def test_summary_starts_with_intent_paragraph_max_three_sentences():
    ev = CodeReviewEvaluation(findings=(), conclusion="success")
    _, summary = cr_dispatch._summary_markdown(
        ev, intent_paragraph=build_intent_paragraph(
            "Add retry to checkout", "For buyers at checkout.", {1: [("open", "retry on 429")]},
        ),
    )
    assert summary.startswith("## Intent")
    paragraph = summary.split("## Intent")[1].split("## Markings")[0].strip()
    sentences = [s for s in re.split(r"[.!?]\s*", paragraph) if s.strip()]
    assert 1 <= len(sentences) <= 3


def test_docs_only_diff_drops_style_but_keeps_wrong_fact():
    findings = (
        _finding("complexity-hotspot", "medium"),
        _finding("dead-code", "low"),
        _finding("doc-code-claim-drift", "medium"),
    )
    assert hunks_are_docs_tests_only.__name__
    assert is_docs_tests_only(("docs/guide.md", "README.md"))
    assert is_docs_tests_only((), ("class:docs",))
    kept = filter_docs_tests_findings(findings)
    rules = {f.rule_name for f in kept}
    assert "doc-code-claim-drift" in rules
    assert "complexity-hotspot" not in rules
    assert "dead-code" not in rules


def test_sensitive_label_forces_full_depth_on_one_line_diff():
    hunks = [Hunk(path="src/x.py", body="@@ -1 +1 @@\n-old\n+new\n")]
    decision = decide_deep_escalation(
        hunks, {"labels": ["class:sensitive"]},  # type: ignore[typeddict-item]
        sample_rate=0.0, diff_line_threshold=10_000,
        path_markers=("no-such-marker-xyz",),
    )
    assert decision.escalate
    assert "class:sensitive" in decision.reasons
    assert should_force_full_depth(("class:sensitive",), added_lines=1)
    paragraph = build_intent_paragraph(
        "Fix auth", "For operators.", None, ("class:sensitive",), ("src/x.py",),
    )
    assert "sensitive" in paragraph.lower()
    assert is_sensitive(("CLASS:SENSITIVE",))


def test_intent_contradicting_criterion_produces_high_mismatch():
    criteria = {7: [("open", "retry the checkout on rate limiting")]}
    mismatch = detect_intent_mismatch(
        "Skip retry", "This change will not retry the checkout; out of scope.", criteria,
    )
    assert mismatch is not None
    assert mismatch.rule_name == "intent-mismatch"
    assert mismatch.severity == "high"
    assert "#7" in mismatch.message


def test_intent_check_ignores_negated_acceptance_criteria_outside_intent():
    body = (
        "## Intent\nClass: logic. Review defensive findings.\n\n"
        "## Summary\nIf no caller can reach the state, do not raise the finding.\n\n"
        "## Acceptance criteria\nA finding without a named caller is dropped."
    )
    criteria = {1166: [("open", "A finding without a named caller is dropped.")]}
    assert detect_intent_mismatch("Review defensive findings", body, criteria) is None
