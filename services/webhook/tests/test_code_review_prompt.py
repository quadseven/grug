"""Tests for code_review_prompt — the Elder structured prompt library.

Verifies the rule set is well-formed (#188 AC) and that the built
system prompt carries the Finding JSON contract the parser expects."""
from __future__ import annotations

import pytest

import code_review_prompt as crp
from review_types import SEVERITIES  # single source (#250)


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
        assert r.severity in SEVERITIES, f"bad severity: {r.name}={r.severity}"


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
    for sev in SEVERITIES:
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
    with pytest.raises(ValueError, match=r"\[A-Za-z0-9_-\]"):
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


def test_rule_post_init_rejects_empty_good_example():
    with pytest.raises(ValueError, match="example"):
        crp.ReviewRule(
            name="x", bug_class="correctness", description="desc here long",
            bad_example="b", good_example="   ", severity="low",
        )


def test_rule_post_init_rejects_empty_bad_example():
    """Left operand of the example guard (bad_example blank, good ok)."""
    with pytest.raises(ValueError, match="example"):
        crp.ReviewRule(
            name="x", bug_class="correctness", description="desc here long",
            bad_example="   ", good_example="g", severity="low",
        )


def test_rule_post_init_rejects_empty_name():
    """`not self.name` operand (distinct from the spaces operand)."""
    with pytest.raises(ValueError, match=r"\[A-Za-z0-9_-\]"):
        crp.ReviewRule(
            name="", bug_class="correctness", description="desc here long",
            bad_example="b", good_example="g", severity="low",
        )


def test_rule_post_init_rejects_empty_description():
    with pytest.raises(ValueError, match="description"):
        crp.ReviewRule(
            name="x", bug_class="correctness", description="   ",
            bad_example="b", good_example="g", severity="low",
        )


def test_inverted_logic_rule_present():
    """Logic-inversion (negated condition / swapped and-or / flipped
    comparison) is a top correctness bug class — must be covered."""
    assert any(r.name == "inverted-logic" for r in crp.RULES)


def test_preamble_biases_toward_precision_and_hardens_injection():
    """The preamble must (a) tell the model to omit low-confidence
    findings (over-reporting guard) and (b) treat diff content as data,
    not instructions (prompt-injection hardening)."""
    p = crp.build_system_prompt().lower()
    assert "false negative" in p and "omit" in p          # precision lever
    assert "at most one rule" in p                         # dedup
    assert "instructions" in p and "data" in p             # injection hardening



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


def test_prompt_teaches_whole_file_mitigation_scan():
    """#336: the Elder is told to scan the WHOLE file for an existing
    mitigation before flagging robustness/security defects (the #1149
    resource-leak false positive)."""
    p = crp.build_system_prompt()
    assert "WHOLE TABLET" in p or "whole file" in p.lower()
    assert "if: always()" in p          # names the exact infra-cleanup shape
    assert "finally" in p


def test_prompt_teaches_path_is_not_secret():
    """#336: a file PATH is not a secret value — closes the #1149 CRITICAL
    secret-in-log false positive."""
    p = crp.build_system_prompt()
    assert "KUBECONFIG=/tmp/x" in p
    assert "not the secret value" in p


def test_new_high_value_rules_present(monkeypatch):
    """#338: the Elder gained 5 high-value bug classes the audit flagged
    as missing from the taxonomy."""
    names = {r.name for r in crp.RULES}
    for rule in ("missing-await", "query-in-loop", "missing-timeout",
                 "unbounded-growth", "missing-pagination"):
        assert rule in names, f"{rule} missing from RULES"
    p = crp.build_system_prompt()
    assert "missing-await" in p and "query-in-loop" in p
    assert "performance" in crp._BUG_CLASSES  # query-in-loop's class


def test_subprocess_no_timeout_rule_present():
    """Weekly harvest: an external/blocking subprocess (or shell node/curl)
    call without a timeout is the runaway-process class — one wedged
    provider hangs the whole chain (claude-stuff #356, #368)."""
    assert any(r.name == "subprocess-no-timeout" for r in crp.RULES)
    assert "subprocess-no-timeout" in crp.build_system_prompt()


def test_monotonic_zero_sentinel_rule_present():
    """Weekly harvest: a throttle/rate-limit sentinel initialized to 0.0 but
    compared against time.monotonic() (since-boot, not epoch) silently drops
    the FIRST event in the first window after boot — fix is float('-inf')
    (grug #450 cf_auth, #444 trace-flush)."""
    assert any(r.name == "monotonic-zero-sentinel" for r in crp.RULES)
    assert "monotonic-zero-sentinel" in crp.build_system_prompt()


def test_voice_has_mandatory_bookend_structure():
    """#343: the voice instruction mandates the structural bookends that
    keep the caveman cadence from slipping to plain English under technical
    load (open in-voice + close 'So speaks Grug.')."""
    p = crp.build_system_prompt()
    assert "So speaks Grug." in p
    assert "STRUCTURE every" in p
    assert "STOP and re-cast" in p          # the anti-plain-English clause
    assert "await fetch_user" in p          # the modern high-density example
    assert "WRAPPER" in p                    # cadence-wraps, core-stays-exact
