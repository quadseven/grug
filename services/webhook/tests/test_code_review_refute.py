"""Refute-gate tests (#714, epic #707).

The gate exists for the semantic-misreading false-positive class that
survived the deterministic verification pass - two same-day production
instances: Elder's inverted-logic claim on grug PR #710 (read a
return-a-reason-kills contract as its opposite) and the inverted-logic
claim on a private repo (refuted by a traced run: 2 calls, not 200).
Both were HIGH severity and sailed through the plausibility judge.
"""

from __future__ import annotations

from personas.code_reviewer import judge as judge_mod
from personas.code_reviewer.judge import partition_refuted, refute_findings
from personas.code_reviewer.persona import Finding
from llm_client import FindingJudgement
from review_evidence import assemble_evidence


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
    out = refute_findings((_finding(),), assemble_evidence(()), 42)
    assert captured["refute"] is True
    assert captured["redact"] is True
    assert captured["count"] == 1
    assert len(out) == 1


def test_refute_findings_fail_open_on_judge_error(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("judge exploded")

    monkeypatch.setattr(judge_mod, "judge_findings", _boom)
    assert refute_findings((_finding(),), assemble_evidence(()), 42) == ()


def test_refute_findings_empty_input_makes_no_call(monkeypatch):
    called = []
    monkeypatch.setattr(judge_mod, "judge_findings", lambda *a, **kw: called.append(1))
    assert refute_findings((), assemble_evidence(()), 42) == ()
    assert called == []


# --- refute fail-open with a confident plain not-real verdict (#1400) -----

from llm_client import JUDGE_BATCH_SIZE  # noqa: E402
from personas.code_reviewer.judge import (  # noqa: E402
    advisory_when_unrefuted, refute_with_outcome,
)


def _fj(index, real, confidence):
    return FindingJudgement(index, real, "reason", confidence)


def _soften(f, *, unavailable=frozenset({0}), plain=None):
    return advisory_when_unrefuted(
        (f,), unavailable, plain_findings=(f,),
        plain_verdicts=plain if plain is not None else (_fj(0, False, 0.92),),
    )


def test_refute_outage_with_plain_not_real_downgrades_a_high():
    """#1400, the live case: every refute backend failed and the plain judge
    had called the high finding not real at 0.92. It still publishes, as a
    non-blocking medium whose marker says why, never hidden."""
    f = _finding()
    out, downgraded = _soften(f)
    assert len(out) == 1
    assert out[0].severity == "medium"
    assert out[0].message.startswith(f.message)
    assert "refute gate was unavailable" in out[0].message
    assert "confidence 0.92" in out[0].message
    assert [(d[0], d[1].confidence) for d in downgraded] == [(f, 0.92)]


def test_refute_outage_at_exactly_the_floor_downgrades():
    out, _ = _soften(_finding(), plain=(_fj(0, False, 0.9),))
    assert out[0].severity == "medium"


def test_refute_outage_below_the_floor_stays_blocking():
    f = _finding()
    for plain in ((_fj(0, False, 0.89),), (_fj(0, True, 0.99),), ()):
        assert _soften(f, plain=plain) == ((f,), ())


def test_critical_stays_blocking_on_a_refute_outage():
    f = _finding(severity="critical")
    assert _soften(f, plain=(_fj(0, False, 0.99),)) == ((f,), ())


def test_answered_finding_stays_blocking():
    """A finding the refute gate answered for is not unavailable: the plain
    judge's not-real can never soften it."""
    f = _finding()
    assert _soften(f, unavailable=frozenset(), plain=(_fj(0, False, 0.99),)) == ((f,), ())


def test_plain_verdict_must_match_the_same_finding():
    f, other = _finding(line=5), _finding(line=7, message="other")
    out, _ = advisory_when_unrefuted(
        (f,), frozenset({0}), plain_findings=(other, f),
        plain_verdicts=(_fj(0, False, 0.99), _fj(1, True, 0.99)),
    )
    assert out == (f,)


def test_refute_gate_judges_every_high_past_the_batch_size(monkeypatch):
    """No truncation: a finding after the first JUDGE_BATCH_SIZE gets its own
    refute verdict, so it can never look unavailable for being late."""
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL", raising=False)
    calls = []

    def _judge(reprs, evidence, **kw):
        calls.append(len(reprs))
        return tuple(_fj(i, True, 0.95) for i in range(len(reprs)))

    monkeypatch.setattr(judge_mod, "judge_findings", _judge)
    findings = tuple(_finding(line=i + 1) for i in range(JUDGE_BATCH_SIZE + 1))
    outcome = refute_with_outcome(findings, assemble_evidence(()), 42)
    assert calls == [JUDGE_BATCH_SIZE, 1]
    assert {v.finding_index for v in outcome.verdicts} == set(range(len(findings)))
    assert outcome.unavailable == frozenset()


def test_index_missing_from_an_answer_is_not_an_outage(monkeypatch):
    """A backend that answered but skipped an index did not fail: that
    finding is not unavailable, so it stays blocking."""
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL", raising=False)
    monkeypatch.setattr(
        judge_mod, "judge_findings", lambda reprs, ev, **kw: (_fj(0, True, 0.95),),
    )
    a, b = _finding(line=5), _finding(line=9)
    outcome = refute_with_outcome((a, b), assemble_evidence(()), 42)
    assert outcome.unavailable == frozenset()
    out, downgraded = advisory_when_unrefuted(
        (a, b), outcome.unavailable, plain_findings=(a, b),
        plain_verdicts=(_fj(0, False, 0.99), _fj(1, False, 0.99)),
    )
    assert out == (a, b) and downgraded == ()


def test_every_backend_failing_marks_the_batch_unavailable(monkeypatch):
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL", raising=False)

    def _boom(*a, **kw):
        raise TimeoutError("cave timed out")

    monkeypatch.setattr(judge_mod, "judge_findings", _boom)
    outcome = refute_with_outcome((_finding(),), assemble_evidence(()), 42)
    assert outcome.verdicts == ()
    assert outcome.unavailable == frozenset({0})
