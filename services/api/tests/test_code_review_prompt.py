"""Tests for code_review_prompt — the Elder structured prompt library.

Verifies the rule set is well-formed (#188 AC) and that the built
system prompt carries the Finding JSON contract the parser expects."""
from __future__ import annotations

import pytest

import code_review_prompt as crp


_VALID_SEVERITIES = {"low", "medium", "high", "critical"}


def test_at_least_15_rules():
    """AC: ≥15 rules extracted from the /audit skill patterns."""
    assert len(crp.RULES) >= 15


def test_rule_names_unique():
    names = [r.name for r in crp.RULES]
    assert len(names) == len(set(names)), "duplicate rule names"


def test_every_rule_is_well_formed():
    """AC: each rule has name, description, good vs bad example, severity."""
    for r in crp.RULES:
        assert r.name and r.name.strip(), f"empty name: {r}"
        # kebab/snake identifier, no spaces — used as the `rule` field.
        assert " " not in r.name, f"rule name has spaces: {r.name}"
        assert r.description and len(r.description) > 10, f"thin description: {r.name}"
        assert r.bad_example and r.bad_example.strip(), f"no bad example: {r.name}"
        assert r.good_example and r.good_example.strip(), f"no good example: {r.name}"
        assert r.severity in _VALID_SEVERITIES, f"bad severity: {r.name}={r.severity}"


def test_rule_is_frozen():
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        crp.RULES[0].name = "mutated"  # type: ignore[misc]


def test_build_system_prompt_includes_every_rule_name():
    """Each rule must actually appear in the rendered prompt — a rule in
    RULES that doesn't reach the LLM is dead weight."""
    prompt = crp.build_system_prompt()
    for r in crp.RULES:
        assert r.name in prompt, f"rule {r.name} missing from prompt"


def test_build_system_prompt_carries_finding_json_contract():
    """The prompt must instruct the exact Finding wire shape the parser
    (`_coerce_finding`) consumes: findings[] with path/line/rule/
    severity/message."""
    prompt = crp.build_system_prompt()
    for token in ("findings", "path", "line", "rule", "severity", "message"):
        assert token in prompt, f"prompt missing Finding field: {token}"
    # All four severities documented so the LLM knows the enum.
    for sev in _VALID_SEVERITIES:
        assert sev in prompt


def test_build_system_prompt_is_deterministic():
    """Stable output — rule order doesn't shuffle between calls (matters
    for DD LLM Obs A/B prompt-variant comparison + cache stability)."""
    assert crp.build_system_prompt() == crp.build_system_prompt()


def test_prompt_demands_json_only_no_prose():
    """The parser does json.loads on the content — the prompt must forbid
    prose outside the JSON object or parse_failed rate spikes."""
    prompt = crp.build_system_prompt().lower()
    assert "json" in prompt
    assert "prose" in prompt or "only" in prompt or "no text" in prompt


def test_rule_post_init_rejects_bad_severity():
    with pytest.raises(ValueError, match="severity"):
        crp.ReviewRule(
            name="x", bug_class="correctness", description="desc here long",
            bad_example="b", good_example="g", severity="hgih",
        )


def test_rule_post_init_rejects_name_with_spaces():
    with pytest.raises(ValueError, match="space-free"):
        crp.ReviewRule(
            name="has spaces", bug_class="correctness",
            description="desc here long", bad_example="b", good_example="g",
            severity="low",
        )


def test_rule_post_init_rejects_unknown_bug_class():
    with pytest.raises(ValueError, match="taxonomy"):
        crp.ReviewRule(
            name="x", bug_class="made-up-class", description="desc here long",
            bad_example="b", good_example="g", severity="low",
        )


def test_rule_post_init_rejects_empty_example():
    with pytest.raises(ValueError, match="example"):
        crp.ReviewRule(
            name="x", bug_class="correctness", description="desc here long",
            bad_example="b", good_example="   ", severity="low",
        )


def test_severity_set_matches_llm_client_no_drift():
    """The prompt's local `_SEVERITIES` must equal llm_client's parser
    set — a rule advertising a severity the parser would drop is a
    silent calibration bug. (The two are separate today to avoid an
    import cycle; this test is the guard until they share a leaf module.)"""
    import llm_client
    assert crp._SEVERITIES == llm_client._VALID_SEVERITIES


def test_covers_core_audit_bug_classes():
    """AC names specific bug classes from the audit stages — spot-check
    the load-bearing ones are present by rule name or description."""
    blob = (
        " ".join(r.name for r in crp.RULES) + " " +
        " ".join(r.description.lower() for r in crp.RULES)
    )
    for needle in (
        "silent",      # silent exception swallowing
        "async",       # sync IO in async paths
        "race",        # race conditions
        "null",        # null-deref
        "mock",        # mock-vs-real exception divergence
    ):
        assert needle in blob.lower(), f"missing audit bug class: {needle}"
