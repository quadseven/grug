"""A free Go model's empty answer as the confirming second opinion.

The default rule stands: a short "no findings" answer is a low-confidence
review unless a second, different, non-free model also answers empty, because
a free model answered empty on a 33k-token diff where the paid primary found
faults. A narrow relaxation exists behind a flag that is off: a free model's
empty answer may confirm only when a paid or ranked model already answered
empty on the same cohort, the cohort is small, the model is on the allowlist
an eval produced, and the flag is on. The HTTP boundary is faked; the rule,
the allowlist and the chain are real.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import llm_client as lc
import opencode_go_free as og
import pytest
from llm_client import Backend, Hunk, review_diff
from personas.code_reviewer.persona import evaluate_diff
from review_evidence import assemble_evidence

PAID = "deepseek-v4.1-flash"
FREE_A = "alpha-free"
FREE_B = "beta-free"
TODAY = date(2026, 11, 14)
STAMP = TODAY.isoformat()


def _stats(scored: int, empty: int) -> dict:
    return {"defect_scored": scored, "false_empty": empty}


def _report(models: dict, when: str = STAMP) -> dict:
    return {"schema": 1, "date": when, "models": models}


# --- the allowlist comes from measured numbers, never from a name ---------------


def test_a_model_qualifies_at_five_percent_on_twenty_defect_cases() -> None:
    assert og.qualified_models(_report({FREE_A: _stats(20, 1)}), today=TODAY) == {FREE_A}


def test_a_model_over_five_percent_does_not_qualify() -> None:
    assert og.qualified_models(_report({FREE_A: _stats(20, 2)}), today=TODAY) == frozenset()
    assert og.qualified_models(_report({FREE_A: _stats(24, 2)}), today=TODAY) == frozenset()


def test_under_twenty_defect_cases_never_qualifies_even_with_no_misses() -> None:
    assert og.qualified_models(_report({FREE_A: _stats(19, 0)}), today=TODAY) == frozenset()


def test_the_stored_allowlist_is_ignored_in_favor_of_the_counts() -> None:
    report = _report({FREE_A: _stats(24, 9)})
    report["allowlist"] = [FREE_A]
    assert og.qualified_models(report, today=TODAY) == frozenset()


def test_a_stale_or_unreadable_report_qualifies_nobody() -> None:
    old = (TODAY - timedelta(days=og.DEFAULT_REPORT_MAX_AGE_DAYS + 1)).isoformat()
    assert og.qualified_models(_report({FREE_A: _stats(24, 0)}, old), today=TODAY) == frozenset()
    bad_reports = (
        None, [], "x", {}, {"models": []},
        {"date": "never", "models": {FREE_A: _stats(24, 0)}},
        _report({FREE_A: {"defect_scored": "24", "false_empty": 0}}),
        _report({FREE_A: {"defect_scored": 24, "false_empty": -1}}),
        _report({FREE_A: _stats(True, 0)}),
    )
    for bad in bad_reports:
        assert og.qualified_models(bad, today=TODAY) == frozenset()


def test_a_missing_or_broken_report_file_loads_as_nothing(tmp_path: Path) -> None:
    assert og.load_eval_report(tmp_path / "absent.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert og.load_eval_report(broken) is None


# --- the rule inside the review chain --------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "test-ocg-key")
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_opencode_go_free_catalog", None, raising=False)
    monkeypatch.setattr(lc, "_opencode_go_free_gate", None, raising=False)
    monkeypatch.setattr(lc, "_spark_data_class_header", lambda: "public")
    monkeypatch.setenv("GRUG_OPENCODE_GO_API_KEY_SSM", "/test/opencode-go")
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    for name in (
        "GRUG_OPENCODE_GO_FALLBACK_MODEL", "GRUG_OPENCODE_GO_SECOND_MODEL",
        "GRUG_CLOUD_FREE_TIER_MODEL", "GRUG_CLOUD_FREE_POOL",
        "GRUG_OPENCODE_GO_FREE_CONFIRM", "GRUG_OPENCODE_GO_FREE_CONFIRM_MAX_TOKENS",
        "GRUG_OPENCODE_GO_FREE_CONFIRM_MAX_AGE_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(lc, "_OPENCODE_GO_MODEL", PAID)
    monkeypatch.setitem(
        lc._BACKEND_CONFIGS, Backend.OPENCODE_GO,
        lc.replace(lc._BACKEND_CONFIGS[Backend.OPENCODE_GO], model=PAID),
    )
    listing = {"object": "list", "data": [
        {"id": m, "type": "chat",
         "cost": {"text": {"input": {"price": price}, "output": {"price": price}}},
         "api": {"chat-completions": {"base_url": "https://example.invalid/v1"}}}
        for m, price in ((FREE_A, 0), (FREE_B, 0), (PAID, 1))
    ]}
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_DISCOVERY", "1")
    monkeypatch.setattr(
        lc, "_opencode_go_free_models_fetch", lambda: httpx.Response(200, json=listing),
        raising=False,
    )
    monkeypatch.setattr(og, "_today", lambda: TODAY, raising=False)
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_CONFIRM_REPORT", str(tmp_path / "report.json"))


def _allow(*models: str, scored: int = 24, empty: int = 0) -> None:
    path = Path(os.environ["GRUG_OPENCODE_GO_FREE_CONFIRM_REPORT"])
    path.write_text(
        json.dumps(_report({m: _stats(scored, empty) for m in models})), encoding="utf-8",
    )


def _flag_on(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_CONFIRM", "1")


def _answer(content: str, prompt_tokens: int = 15_000, completion_tokens: int = 7) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    })


_EMPTY = '{"findings": []}'
_FOUND = (
    '{"findings": [{"path": "src/x.py", "line": 1, "rule": "r", '
    '"severity": "medium", "message": "m"}]}'
)


def _hunk(chars: int) -> Hunk:
    return Hunk(path="src/x.py", body="@@ -1 +1 @@\n+" + "y" * chars)


def _run(paid_says: str | None, free_says: dict[str, str], chars: int = 9_000):
    """Review one cohort. The paid primary answers `paid_says` (None: it
    fails to connect); each free model answers its own text; anything else
    fails to connect. Returns the review and the models asked, in order."""
    asked: list[str] = []

    def respond(_url, **kwargs) -> httpx.Response:
        model = kwargs["json"].get("model", "")
        asked.append(model)
        if model == PAID and paid_says is not None:
            return _answer(paid_says)
        if model in free_says:
            return _answer(free_says[model])
        raise httpx.ConnectError("down")

    with patch.object(httpx, "post", side_effect=respond):
        out = review_diff(assemble_evidence([_hunk(chars)]), installation_id=1)
    return out, asked


def _low_confidence(out) -> bool:
    return out.kind == "reviewed" and (out.error or "").startswith("low confidence:")


def test_the_default_rule_a_free_models_empty_answer_confirms_nothing() -> None:
    """Flag off, no report: the paid primary's empty answer stays low confidence."""
    out, asked = _run(_EMPTY, {FREE_A: _EMPTY, FREE_B: _EMPTY})
    assert _low_confidence(out)
    assert evaluate_diff((), out).conclusion == "neutral"
    assert FREE_A in asked and FREE_B in asked


def test_flag_off_changes_nothing_even_with_an_allowlist() -> None:
    _allow(FREE_A, FREE_B)
    out, asked = _run(_EMPTY, {FREE_A: _EMPTY, FREE_B: _EMPTY})
    assert _low_confidence(out)
    assert FREE_B in asked  # the chain walked on: nothing was confirmed


def test_flag_on_without_a_report_changes_nothing(monkeypatch) -> None:
    _flag_on(monkeypatch)
    out, _ = _run(_EMPTY, {FREE_A: _EMPTY, FREE_B: _EMPTY})
    assert _low_confidence(out)


def test_an_allowlisted_free_empty_and_a_paid_empty_are_a_clean_review(monkeypatch, caplog) -> None:
    _flag_on(monkeypatch)
    _allow(FREE_A)
    with caplog.at_level("INFO"):
        out, asked = _run(_EMPTY, {FREE_A: _EMPTY, FREE_B: _EMPTY})
    assert out.kind == "reviewed"
    assert out.findings == ()
    assert not out.error
    assert evaluate_diff((), out).conclusion == "success"
    assert asked == [FREE_A, FREE_B, PAID]
    assert out.model_name == FREE_A
    assert any(r.getMessage() == "llm_cloud_empty_consensus" for r in caplog.records)


def test_a_model_not_on_the_allowlist_never_confirms(monkeypatch) -> None:
    _flag_on(monkeypatch)
    _allow(FREE_B)
    out, asked = _run(_EMPTY, {FREE_A: _EMPTY, FREE_B: _EMPTY})
    # FREE_A may not confirm and is passed over; FREE_B is listed, so it does.
    assert asked == [FREE_A, FREE_B, PAID]
    assert out.model_name == FREE_B
    _allow("some-other-free")
    out, _ = _run(_EMPTY, {FREE_A: _EMPTY, FREE_B: _EMPTY})
    assert _low_confidence(out)


def test_a_model_that_misses_the_bar_in_the_report_never_confirms(monkeypatch) -> None:
    _flag_on(monkeypatch)
    _allow(FREE_A, FREE_B, scored=24, empty=6)
    out, _ = _run(_EMPTY, {FREE_A: _EMPTY, FREE_B: _EMPTY})
    assert _low_confidence(out)


def test_a_large_cohort_never_relaxes(monkeypatch) -> None:
    _flag_on(monkeypatch)
    _allow(FREE_A, FREE_B)
    # A cohort over 10,000 tokens (about 40,000 characters) is the regime where
    # a free model answered empty and the paid primary found faults.
    out, _ = _run(_EMPTY, {FREE_A: _EMPTY}, chars=40_100)  # just over 10,000 tokens
    assert _low_confidence(out)
    out, _ = _run(_EMPTY, {FREE_A: _EMPTY}, chars=39_000)  # just under
    assert not out.error


def test_the_cohort_limit_is_configurable(monkeypatch) -> None:
    _flag_on(monkeypatch)
    _allow(FREE_A)
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_CONFIRM_MAX_TOKENS", "1000")
    out, _ = _run(_EMPTY, {FREE_A: _EMPTY})
    assert _low_confidence(out)  # a 9,000 character cohort is over 1,000 tokens
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_CONFIRM_MAX_TOKENS", "not a number")
    out, _ = _run(_EMPTY, {FREE_A: _EMPTY})
    assert not out.error  # a bad value falls back to the default


def test_no_paid_empty_means_no_relaxation(monkeypatch) -> None:
    _flag_on(monkeypatch)
    _allow(FREE_A, FREE_B)
    # The paid primary is down; two free models answer empty. Two free answers
    # are still not a clean review.
    out, asked = _run(None, {FREE_A: _EMPTY, FREE_B: _EMPTY})
    assert _low_confidence(out)
    assert FREE_A in asked and FREE_B in asked


def test_a_paid_answer_with_findings_is_never_overridden(monkeypatch) -> None:
    _flag_on(monkeypatch)
    _allow(FREE_A)
    out, asked = _run(_FOUND, {FREE_A: _EMPTY})
    assert len(out.findings) == 1
    assert asked == [FREE_A, FREE_B, PAID]


def test_a_free_models_findings_still_win_over_a_paid_skim(monkeypatch) -> None:
    _flag_on(monkeypatch)
    _allow(FREE_A)
    out, _ = _run(_EMPTY, {FREE_A: _FOUND})
    assert len(out.findings) == 1


def test_the_pure_rule_needs_a_known_cohort_size(monkeypatch) -> None:
    """Called outside a review the diff size is unknown, and unknown never relaxes."""
    _flag_on(monkeypatch)
    _allow(FREE_A)
    paid, free = (Backend.OPENCODE_GO, PAID), (Backend.OPENCODE_GO, FREE_A)
    lc._opencode_go_free_catalog_get().models()
    assert not lc._empty_answers_agree(paid, free, None)
    with lc._reviewed_diff_scope([_hunk(9_000)]):
        assert lc._empty_answers_agree(paid, free, None)
        # Never the other way round: a free first answer is not a paid one.
        assert not lc._empty_answers_agree(free, paid, None)
        # Never a free model confirming a free model.
        assert not lc._empty_answers_agree((Backend.OPENCODE_GO, FREE_B), free, None)
