"""#191 — Elder prompt A/B experiment: variant build + selection + mode read.

Three units under test, each a pure-ish seam so the experiment is testable
without a live SSM or LLM:

  - `code_review_prompt.build_system_prompt(variant)` — assembles the system
    prompt from a shared head/tail + a variant-specific confidence clause.
    v1 (precision-biased) is byte-identical to the shipped prompt; v2
    (recall-biased) is the new arm.
  - `llm_client.select_prompt_variant(installation_id)` — maps an install to
    an arm given the experiment mode. The split bucket MUST be orthogonal to
    the backend split (which uses `% 2`) so the A/B isn't confounded by which
    LLM answered.
  - `secrets_loader.get_prompt_experiment_mode()` — reads the mode from SSM,
    fallback-safe to "off" so a missing/broken param degrades to the shipped
    v1 prompt rather than breaking review.
"""
from __future__ import annotations

import code_review_prompt as crp
import llm_client as lc
import secrets_loader as sl


# --- build_system_prompt(variant) -----------------------------------------

def test_default_variant_is_v1_and_byte_identical() -> None:
    """No-arg call == explicit v1. v1 must be the shipped prompt unchanged so
    #191 introduces ZERO behavior change for the off/control population."""
    assert crp.build_system_prompt() == crp.build_system_prompt("v1")


def test_v2_differs_from_v1() -> None:
    assert crp.build_system_prompt("v2") != crp.build_system_prompt("v1")


def test_v1_is_precision_biased_v2_is_recall_biased() -> None:
    """The two arms differ only in the confidence clause: v1 prefers a false
    negative (omit when unsure), v2 surfaces at MEDIUM confidence."""
    v1, v2 = crp.build_system_prompt("v1"), crp.build_system_prompt("v2")
    assert "false negative" in v1.lower()
    assert "false negative" not in v2.lower()
    assert "medium" in v2.lower()


def test_both_variants_keep_shared_head_and_tail() -> None:
    """Only the confidence clause swaps — the rule catalogue, the JSON output
    contract, and the prompt-injection hardening (the load-bearing safety
    text) must survive in BOTH arms, or v2 silently drops a guardrail."""
    for variant in ("v1", "v2"):
        prompt = crp.build_system_prompt(variant)
        assert "json" in prompt.lower()          # output contract
        assert "at most one rule" in prompt.lower()  # tail rule
        assert "instruction" in prompt.lower()   # injection hardening


def test_confidence_clauses_cover_exactly_the_variants() -> None:
    """Pin the one hand-maintained variant-set source: every `PromptVariant`
    member has a confidence clause and vice-versa. Mirrors the SEVERITIES /
    Severity pin in test_review_types — locks the import-time assert so a
    deleted guard or a new arm-without-a-clause fails a test, not just import."""
    from typing import get_args
    assert set(crp._CONFIDENCE_CLAUSES) == set(get_args(crp.PromptVariant))


def test_unknown_variant_raises() -> None:
    """A typo'd arm must fail loudly at call time, not silently fall back to
    v1 (which would make a misconfigured experiment look like it's running)."""
    try:
        crp.build_system_prompt("v3")  # type: ignore[arg-type]
    except ValueError as e:
        assert "v3" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown variant")


# --- select_prompt_variant(installation_id) --------------------------------

def _force_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(lc, "get_prompt_experiment_mode", lambda: mode)


def test_mode_off_always_v1(monkeypatch) -> None:
    _force_mode(monkeypatch, "off")
    assert all(lc.select_prompt_variant(i) == "v1" for i in range(8))


def test_mode_all_v2_always_v2(monkeypatch) -> None:
    _force_mode(monkeypatch, "all_v2")
    assert all(lc.select_prompt_variant(i) == "v2" for i in range(8))


def test_mode_split_buckets_by_id(monkeypatch) -> None:
    """Split assigns by `(id // 2) % 2`: ids 0,1 → v1; 2,3 → v2; 4,5 → v1…"""
    _force_mode(monkeypatch, "split")
    assert [lc.select_prompt_variant(i) for i in range(8)] == [
        "v1", "v1", "v2", "v2", "v1", "v1", "v2", "v2",
    ]


def test_split_is_orthogonal_to_backend(monkeypatch) -> None:
    """The prompt arm must not correlate with the backend arm, or the A/B is
    confounded (a v2 win could just be a Poolside win). Backend uses `% 2`;
    variant uses `(// 2) % 2`. Over 0..15 the two splits must be uncorrelated:
    each (backend, variant) cell holds an equal count."""
    _force_mode(monkeypatch, "split")
    from collections import Counter
    cells: Counter = Counter()
    for i in range(64):  # 16 full periods — robust to a later period change
        backend = "even" if i % 2 == 0 else "odd"   # mirrors select_backend
        cells[(backend, lc.select_prompt_variant(i))] += 1
    assert set(cells.values()) == {16}, cells  # 2x2 grid, equal cells → independent


def test_unknown_mode_falls_back_to_v1(monkeypatch) -> None:
    """A garbage mode value (someone fat-fingered the SSM param) must not
    crash and must not flip anyone to v2 — degrade to control."""
    _force_mode(monkeypatch, "splitt")  # typo
    assert all(lc.select_prompt_variant(i) == "v1" for i in range(8))


# --- get_prompt_experiment_mode() ------------------------------------------

def test_mode_missing_env_returns_off(monkeypatch) -> None:
    sl.get_prompt_experiment_mode.cache_clear()
    monkeypatch.delenv("GRUG_PROMPT_EXPERIMENT_SSM", raising=False)
    assert sl.get_prompt_experiment_mode() == "off"


def test_mode_reads_ssm_value(monkeypatch) -> None:
    sl.get_prompt_experiment_mode.cache_clear()
    monkeypatch.setenv("GRUG_PROMPT_EXPERIMENT_SSM", "/grug/elder-prompt-experiment")
    monkeypatch.setattr(
        sl._ssm, "get_parameter",
        lambda *, Name: {"Parameter": {"Value": "split"}},
    )
    assert sl.get_prompt_experiment_mode() == "split"


def test_mode_unrecognized_value_degrades_to_off(monkeypatch) -> None:
    """A fetched-but-garbage value (operator typo) degrades to off — and the
    loader, not just select_prompt_variant, rejects it so the warning fires."""
    sl.get_prompt_experiment_mode.cache_clear()
    monkeypatch.setenv("GRUG_PROMPT_EXPERIMENT_SSM", "/grug/elder-prompt-experiment")
    monkeypatch.setattr(
        sl._ssm, "get_parameter",
        lambda *, Name: {"Parameter": {"Value": "splitt"}},
    )
    assert sl.get_prompt_experiment_mode() == "off"
    sl.get_prompt_experiment_mode.cache_clear()


def test_mode_value_is_stripped(monkeypatch) -> None:
    """A console-pasted value with trailing whitespace still matches its arm."""
    sl.get_prompt_experiment_mode.cache_clear()
    monkeypatch.setenv("GRUG_PROMPT_EXPERIMENT_SSM", "/grug/elder-prompt-experiment")
    monkeypatch.setattr(
        sl._ssm, "get_parameter",
        lambda *, Name: {"Parameter": {"Value": "  all_v2\n"}},
    )
    assert sl.get_prompt_experiment_mode() == "all_v2"
    sl.get_prompt_experiment_mode.cache_clear()


def test_mode_ssm_error_degrades_to_off(monkeypatch) -> None:
    """The #253 lesson: a missing/unreadable param must degrade, not raise —
    the experiment must never break a review."""
    sl.get_prompt_experiment_mode.cache_clear()
    monkeypatch.setenv("GRUG_PROMPT_EXPERIMENT_SSM", "/grug/elder-prompt-experiment")

    def _boom(*, Name):
        raise RuntimeError("ssm down")

    monkeypatch.setattr(sl._ssm, "get_parameter", _boom)
    assert sl.get_prompt_experiment_mode() == "off"
    sl.get_prompt_experiment_mode.cache_clear()
