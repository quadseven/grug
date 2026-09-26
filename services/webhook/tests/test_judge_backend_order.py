"""Judge backend order (operator decision 2026-09-26: the Sparks overheat).

Under cloud priority the redacted cloud judges go first and the in-cluster
Cave judge is the last resort. A partial answer is a failed attempt, never
a verdict. Under cave priority the order is unchanged.
"""
from __future__ import annotations

from llm_client import Backend, FindingJudgement, Hunk
from personas.code_reviewer import judge


_FINDINGS = [{"rule_name": "r", "file": "src/x.py", "line": 1,
              "severity": "low", "message": "m"}]


def _verdict() -> FindingJudgement:
    return FindingJudgement(finding_index=0, is_real_bug=True, confidence=0.9, reasoning="ok")


def _run(monkeypatch, answers: dict) -> list:
    calls: list = []

    def fake_judge(findings, hunks, *, config=None, redact=False, **kw):
        backend = config.backend if config is not None else None
        calls.append((backend, redact))
        return answers.get(backend, ())

    monkeypatch.setattr(judge, "judge_findings", fake_judge)
    judge._judge_evidence_packet(
        _FINDINGS, [Hunk(path="src/x.py", body="@@ -1 +1 @@\n+x")], 1,
        pr_context=None, file_contents=None, cross_file_contents=None,
        runtime_context=None,
    )
    return calls


def _cloud(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", "z-ai/glm-5.2:free")


def test_cloud_priority_cloud_judge_answers_and_cave_is_never_called(monkeypatch):
    _cloud(monkeypatch)
    calls = _run(monkeypatch, {Backend.POOLSIDE: (_verdict(),)})
    assert calls == [(Backend.POOLSIDE, True)]


def test_cloud_priority_cave_is_the_last_resort(monkeypatch):
    _cloud(monkeypatch)
    calls = _run(monkeypatch, {Backend.CAVE: (_verdict(),)})
    assert [c[0] for c in calls] == [Backend.POOLSIDE, Backend.OPENROUTER, Backend.CAVE]
    assert calls[0][1] and calls[1][1], "cloud judges get redacted evidence"


def test_cave_priority_keeps_cave_first(monkeypatch):
    monkeypatch.delenv("GRUG_REVIEW_BACKEND_PRIORITY", raising=False)
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    calls = _run(monkeypatch, {Backend.CAVE: (_verdict(),)})
    assert calls == [(Backend.CAVE, False)]
