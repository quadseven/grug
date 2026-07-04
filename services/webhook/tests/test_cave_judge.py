"""In-house Cave exposed-secret judge routing (#439, ADR-0009).

The exposed-secret class routes to the in-cluster spark-gateway (raw
value never leaves the boundary); remaining classes go to the SaaS judge
with REDACTED input. Everything fail-opens to today's behavior when the
gateway is unconfigured, and a Cave outage falls back to SaaS so secret
DETECTION never regresses.
"""
from __future__ import annotations

from unittest.mock import patch

import llm_client
from llm_client import Backend, FindingJudgement, Hunk, _build_judge_messages, _cave_judge_config
from personas.code_reviewer import sast
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.sast import EXPOSED_SECRET, Candidate, judge_candidates


_DIFF = """diff --git a/.env b/.env
--- a/.env
+++ b/.env
@@ -0,0 +1,2 @@
+AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE
+x = query(user_input)
"""


def _hunks():
    return parse_diff(_DIFF)


def test_cave_config_none_when_unset(monkeypatch):
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL", raising=False)
    assert _cave_judge_config() is None


def test_cave_config_builds_from_env(monkeypatch):
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://gw.example.svc:8080/")
    monkeypatch.setenv("GRUG_CAVE_JUDGE_MODEL", "test-model:latest")
    cfg = _cave_judge_config()
    assert cfg is not None
    assert cfg.backend is Backend.CAVE
    assert cfg.url == "http://gw.example.svc:8080/v1/chat/completions"
    assert cfg.model == "test-model:latest"
    assert cfg.key_loader()  # non-empty placeholder (gateway unauthenticated)


def test_judge_messages_redact_masks_secret():
    reprs = [{"rule_name": "sqli", "file": "a.py", "line": 2,
              "severity": "high", "message": "m"}]
    hunks = [Hunk(path=".env", body="+AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7REALKEY")]
    raw = _build_judge_messages(reprs, hunks)[1]["content"]
    redacted = _build_judge_messages(reprs, hunks, redact=True)[1]["content"]
    assert "AKIAIOSFODNN7REALKEY" in raw
    assert "AKIAIOSFODNN7REALKEY" not in redacted


def test_secrets_route_to_cave_others_to_redacted_saas(monkeypatch):
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://gw.example.svc:8080")
    calls = []

    def fake_judge(reprs, hunks, installation_id, pr_context=None,
                   file_contents=None, *, config=None, redact=False):
        calls.append({
            "rules": [r["rule_name"] for r in reprs],
            "backend": getattr(getattr(config, "backend", None), "value", "saas-default"),
            "redact": redact,
        })
        return tuple(
            FindingJudgement(i, True, "real", 0.9) for i in range(len(reprs))
        )

    monkeypatch.setattr(sast, "judge_findings", fake_judge)
    cands = (
        Candidate(EXPOSED_SECRET, ".env", 1, "AWS key (masked)"),
        Candidate("sql-injection", "a.py", 2, "query(user_input)"),
    )
    kept = judge_candidates(cands, _hunks(), 1)
    assert len(kept) == 2
    cave_calls = [c for c in calls if c["backend"] == "cave"]
    saas_calls = [c for c in calls if c["backend"] == "saas-default"]
    assert len(cave_calls) == 1 and cave_calls[0]["rules"] == [EXPOSED_SECRET]
    assert cave_calls[0]["redact"] is False  # Cave needs the raw value
    assert len(saas_calls) == 1 and saas_calls[0]["rules"] == ["sql-injection"]
    assert saas_calls[0]["redact"] is True  # 2d: SaaS classes redacted


def test_unconfigured_cave_keeps_single_saas_call_unredacted_with_secrets(monkeypatch):
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL", raising=False)
    calls = []

    def fake_judge(reprs, hunks, installation_id, pr_context=None,
                   file_contents=None, *, config=None, redact=False):
        calls.append({"n": len(reprs), "config": config, "redact": redact})
        return tuple(FindingJudgement(i, True, "r", 0.9) for i in range(len(reprs)))

    monkeypatch.setattr(sast, "judge_findings", fake_judge)
    cands = (
        Candidate(EXPOSED_SECRET, ".env", 1, "AWS key (masked)"),
        Candidate("sql-injection", "a.py", 2, "s"),
    )
    kept = judge_candidates(cands, _hunks(), 1)
    # Parity with today: ONE call, default backend, unredacted (the secret
    # candidate still needs its raw value on the SaaS path).
    assert len(kept) == 2 and len(calls) == 1
    assert calls[0]["config"] is None and calls[0]["redact"] is False


def test_no_secrets_saas_call_is_redacted(monkeypatch):
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL", raising=False)
    calls = []

    def fake_judge(reprs, hunks, installation_id, pr_context=None,
                   file_contents=None, *, config=None, redact=False):
        calls.append({"redact": redact})
        return (FindingJudgement(0, True, "r", 0.9),)

    monkeypatch.setattr(sast, "judge_findings", fake_judge)
    kept = judge_candidates(
        (Candidate("sql-injection", "a.py", 2, "s"),), _hunks(), 1,
    )
    assert len(kept) == 1
    assert calls[0]["redact"] is True  # 2d applies even pre-Cave


def test_cave_outage_fails_closed_no_saas_leak(monkeypatch):
    """Codex PR #486 round 2: once the in-cluster boundary is CONFIGURED,
    a Cave outage suppresses the secret batch for this pass - it must
    NEVER retry on SaaS with the raw value (the outage moment is exactly
    when the privacy control matters). Next push/rerun heals; the
    monitored log line alerts."""
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://gw.example.svc:8080")
    calls = []

    def fake_judge(reprs, hunks, installation_id, pr_context=None,
                   file_contents=None, *, config=None, redact=False):
        backend = getattr(getattr(config, "backend", None), "value", "saas")
        calls.append(backend)
        if backend == "cave":
            return ()  # the judge error shape
        return tuple(FindingJudgement(i, True, "r", 0.9) for i in range(len(reprs)))

    monkeypatch.setattr(sast, "judge_findings", fake_judge)
    kept = judge_candidates(
        (Candidate(EXPOSED_SECRET, ".env", 1, "AWS key (masked)"),), _hunks(), 1,
    )
    assert kept == ()          # fail-closed for this pass
    assert calls == ["cave"]   # the raw batch never reached SaaS


def test_real_judge_findings_uses_cave_backend(monkeypatch):
    """Codex PR #486 CRITICAL regression: call the REAL judge_findings
    with a Cave config, mocking only _call_backend - the override path
    must reach the Cave URL (no UnboundLocalError, no silent SaaS
    fallback)."""
    import httpx as _httpx

    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://gw.example.svc:8080")
    cfg = _cave_judge_config()
    seen = {}

    def fake_call_backend(config, messages):
        seen["url"] = config.url
        seen["backend"] = config.backend.value
        return _httpx.Response(
            200,
            json={"choices": [{"message": {"content":
                '{"verdicts": [{"index": 0, "is_real_bug": true, '
                '"confidence": 0.9, "reasoning": "live key"}]}'}}]},
            request=_httpx.Request("POST", config.url),
        )

    monkeypatch.setattr(llm_client, "_call_backend", fake_call_backend)
    reprs = [{"rule_name": EXPOSED_SECRET, "file": ".env", "line": 1,
              "severity": "high", "message": "masked"}]
    verdicts = llm_client.judge_findings(
        reprs, [Hunk(path=".env", body="+k=v")], 1, config=cfg,
    )
    assert seen["backend"] == "cave"
    assert seen["url"].startswith("http://gw.example.svc:8080")
    assert len(verdicts) == 1 and verdicts[0].is_real_bug is True


def test_cave_all_suppressed_does_not_retry_on_saas(monkeypatch):
    """Codex PR #486 HIGH regression: a Cave batch whose verdicts mark
    EVERY secret candidate not-real is a SUCCESSFUL judgement (kept=()),
    not an outage - the raw batch must NOT retry on SaaS."""
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://gw.example.svc:8080")
    backends_called = []

    def fake_judge(reprs, hunks, installation_id, pr_context=None,
                   file_contents=None, *, config=None, redact=False):
        backend = getattr(getattr(config, "backend", None), "value", "saas")
        backends_called.append(backend)
        if backend == "cave":
            # Complete verdicts: every candidate judged NOT real.
            return tuple(
                FindingJudgement(i, False, "docs example", 0.95)
                for i in range(len(reprs))
            )
        return tuple(FindingJudgement(i, True, "r", 0.9) for i in range(len(reprs)))

    monkeypatch.setattr(sast, "judge_findings", fake_judge)
    kept = judge_candidates(
        (Candidate(EXPOSED_SECRET, ".env", 1, "AWS key (masked)"),), _hunks(), 1,
    )
    assert kept == ()                    # suppression honored
    assert backends_called == ["cave"]   # NO SaaS retry with the raw value
