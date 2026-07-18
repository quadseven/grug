"""Refute-gate tests (#714, epic #707).

The gate exists for the semantic-misreading false-positive class that
survived the deterministic verification pass - two same-day production
instances: Elder's inverted-logic claim on grug PR #710 (read a
return-a-reason-kills contract as its opposite) and the inverted-logic
claim on digital-ledger#208 (refuted by a traced run: 2 calls, not 200).
Both were HIGH severity and sailed through the plausibility judge.
"""

from __future__ import annotations

from personas.code_reviewer import judge as judge_mod
from personas.code_reviewer.judge import partition_refuted, refute_findings
from personas.code_reviewer.persona import Finding
from llm_client import FindingJudgement


def _finding(**kw) -> Finding:
    base = dict(
        file="services/x.py",
        line=5,
        severity="high",
        rule_name="inverted-logic",
        message="the condition keeps findings only when suggestions are present",
        suggestion=None,
    )
    base.update(kw)
    return Finding(**base)


# --- partition_refuted (pure) ---------------------------------------------


def test_confident_refutation_kills():
    """The PR #710 fixture: an inverted-logic claim the refuter grounds as
    backwards gets killed at high confidence."""
    f = _finding()
    kept, refuted = partition_refuted(
        (f,), (FindingJudgement(0, False, "quoted code shows the opposite", 0.95),),
    )
    assert kept == ()
    assert refuted == (f,)


def test_low_confidence_refutation_publishes():
    f = _finding()
    kept, refuted = partition_refuted(
        (f,), (FindingJudgement(0, False, "probably wrong but unclear", 0.6),),
    )
    assert kept == (f,)
    assert refuted == ()


def test_confirmed_claim_publishes():
    f = _finding()
    kept, refuted = partition_refuted(
        (f,), (FindingJudgement(0, True, "quoted lines exhibit the defect", 0.9),),
    )
    assert kept == (f,)


def test_no_verdict_publishes():
    """Gate outage / index miss = fail-open keep."""
    f = _finding()
    kept, refuted = partition_refuted((f,), ())
    assert kept == (f,)
    kept, refuted = partition_refuted(
        (f,), (FindingJudgement(7, False, "index out of range", 0.99),),
    )
    assert kept == (f,)


def test_first_verdict_per_index_wins():
    f = _finding()
    kept, refuted = partition_refuted(
        (f,),
        (
            FindingJudgement(0, True, "confirmed", 0.9),
            FindingJudgement(0, False, "second vote must not kill", 0.99),
        ),
    )
    assert kept == (f,)


# --- refute_findings (thin IO wrapper) ------------------------------------


def test_refute_findings_calls_judge_with_refute_flag(monkeypatch):
    captured = {}

    def _fake_judge(reprs, hunks, **kw):
        captured.update(kw)
        captured["count"] = len(reprs)
        return (FindingJudgement(0, False, "refuted", 0.95),)

    monkeypatch.setattr(judge_mod, "judge_findings", _fake_judge)
    out = refute_findings((_finding(),), (), 42)
    assert captured["refute"] is True
    assert captured["redact"] is True
    assert captured["count"] == 1
    assert len(out) == 1


def test_refute_findings_fail_open_on_judge_error(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("judge exploded")

    monkeypatch.setattr(judge_mod, "judge_findings", _boom)
    assert refute_findings((_finding(),), (), 42) == ()


def test_refute_findings_empty_input_makes_no_call(monkeypatch):
    called = []
    monkeypatch.setattr(judge_mod, "judge_findings", lambda *a, **kw: called.append(1))
    assert refute_findings((), (), 42) == ()
    assert called == []
