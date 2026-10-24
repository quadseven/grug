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

from personas.code_reviewer.judge import advisory_when_unrefuted  # noqa: E402


def _fj(index, real, confidence):
    return FindingJudgement(index, real, "reason", confidence)


def test_unrefuted_high_the_judge_called_not_real_becomes_advisory():
    """#1400, the live case: every refute backend failed (no verdict) and
    the plain judge had called the high finding not real at 0.92. It still
    publishes, as a non-blocking medium carrying a note, never hidden."""
    f = _finding()
    out, downgraded = advisory_when_unrefuted(
        (f,), (), plain_findings=(f,), plain_verdicts=(_fj(0, False, 0.92),),
    )
    assert len(out) == 1
    assert out[0].severity == "medium"
    assert out[0].message.startswith(f.message)
    assert "0.92" in out[0].message
    assert [(d[0], d[1].confidence) for d in downgraded] == [(f, 0.92)]


def test_partial_refute_answer_downgrades_only_the_unanswered_finding():
    a, b = _finding(line=5), _finding(line=9)
    out, downgraded = advisory_when_unrefuted(
        (a, b), (_fj(0, True, 0.95),),
        plain_findings=(a, b),
        plain_verdicts=(_fj(0, False, 0.95), _fj(1, False, 0.95)),
    )
    assert out[0] is a
    assert out[1].severity == "medium"
    assert [d[0] for d in downgraded] == [b]


def test_high_stays_blocking_when_the_refute_gate_answered():
    """A refute verdict that ran always wins: it upheld the finding (or was
    unsure), so the plain judge's not-real cannot soften it."""
    f = _finding()
    for refute in ((_fj(0, True, 0.95),), (_fj(0, False, 0.5),)):
        out, downgraded = advisory_when_unrefuted(
            (f,), refute, plain_findings=(f,),
            plain_verdicts=(_fj(0, False, 0.99),),
        )
        assert out == (f,) and downgraded == ()


def test_high_stays_blocking_without_a_confident_plain_not_real():
    f = _finding()
    for plain in ((_fj(0, True, 0.99),), (_fj(0, False, 0.89),), ()):
        out, downgraded = advisory_when_unrefuted(
            (f,), (), plain_findings=(f,), plain_verdicts=plain,
        )
        assert out == (f,) and downgraded == ()


def test_plain_verdict_must_match_the_same_finding():
    """The plain verdict index refers to the judge's own finding list; a
    not-real verdict on a different finding never softens this one."""
    f, other = _finding(line=5), _finding(line=7, message="other")
    out, _ = advisory_when_unrefuted(
        (f,), (), plain_findings=(other, f),
        plain_verdicts=(_fj(0, False, 0.99), _fj(1, True, 0.99)),
    )
    assert out == (f,)
