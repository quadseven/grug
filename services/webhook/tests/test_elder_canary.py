"""Elder review canary: verdict logic, dense emission, hourly gate, isolation."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import poller_handler
import pytest
from llm_client import Backend, Finding, LlmReviewResponse
from personas.code_reviewer import canary


def _finding(line: int, severity="high", rule="sql-injection", message="user input in query"):
    return Finding(path=canary.CANARY_PATH, line=line, rule=rule, severity=severity, message=message)


def _reviewed(*findings, error=""):
    return LlmReviewResponse(
        kind="reviewed", findings=tuple(findings), backend_used=Backend.OPENROUTER,
        model_name="m-1", error=error,
    )


@pytest.fixture
def emitted(monkeypatch):
    calls: list[tuple[str, float, dict]] = []
    monkeypatch.setattr(canary, "emit_gauge", lambda m, v, tags=None: calls.append((m, v, tags)))
    return calls


# ---- fixtures ----------------------------------------------------------

def test_fixtures_differ_only_in_the_query_and_planted_line_is_the_concat():
    planted = canary.PLANTED_HUNKS[0].body.splitlines()
    clean = canary.CLEAN_HUNKS[0].body.splitlines()
    assert len(planted) == len(clean) and len(planted) > 40
    diff = [i for i, (a, b) in enumerate(zip(planted, clean, strict=True)) if a != b]
    assert len(diff) == 1
    # body line 0 is the @@ header, so file line N is body index N.
    assert diff[0] == canary.PLANTED_LINE
    assert "'\" + name + \"'" in planted[canary.PLANTED_LINE]
    assert "%s" in clean[canary.PLANTED_LINE]


# ---- verdicts ----------------------------------------------------------

@pytest.mark.parametrize("delta", [-2, 0, 2])
def test_planted_caught_within_two_lines(delta):
    r = _reviewed(_finding(canary.PLANTED_LINE + delta))
    assert canary.planted_verdict(r) == "caught"


def test_planted_missed_when_finding_far_away():
    assert canary.planted_verdict(_reviewed(_finding(canary.PLANTED_LINE + 3))) == "missed"


def test_planted_missed_when_severity_low():
    assert canary.planted_verdict(_reviewed(_finding(canary.PLANTED_LINE, severity="medium"))) == "missed"


def test_planted_missed_when_unrelated_rule():
    f = _finding(canary.PLANTED_LINE, rule="naming", message="rename variable")
    assert canary.planted_verdict(_reviewed(f)) == "missed"


def test_planted_caught_by_message_alone_and_critical():
    f = _finding(canary.PLANTED_LINE, severity="critical", rule="security", message="SQL built from input")
    assert canary.planted_verdict(_reviewed(f)) == "caught"


def test_planted_missed_when_no_findings():
    assert canary.planted_verdict(_reviewed()) == "missed"


@pytest.mark.parametrize("kind", ["parse_failed", "all_failed", "no_diff"])
def test_degraded_is_error_for_both_cases(kind):
    r = LlmReviewResponse(kind=kind, error="boom")
    assert canary.planted_verdict(r) == "error"
    assert canary.clean_verdict(r) == "error"


def test_partial_review_without_a_catch_is_error_not_missed():
    assert canary.planted_verdict(_reviewed(error="partial review: 1 cohort failed")) == "error"


def test_partial_review_with_a_catch_is_still_caught():
    r = _reviewed(_finding(canary.PLANTED_LINE), error="partial review: x")
    assert canary.planted_verdict(r) == "caught"


def test_clean_pass_and_false_positive():
    assert canary.clean_verdict(_reviewed()) == "pass"
    assert canary.clean_verdict(_reviewed(_finding(5, severity="medium"))) == "pass"
    assert canary.clean_verdict(_reviewed(_finding(5, severity="high"))) == "false_positive"
    assert canary.clean_verdict(_reviewed(_finding(5, severity="critical"))) == "false_positive"


def test_clean_partial_review_is_error():
    assert canary.clean_verdict(_reviewed(error="partial review: x")) == "error"


# ---- emission ----------------------------------------------------------

def _run(monkeypatch, planted, clean):
    def fake(hunks, installation_id, pr_context=None, **kw):
        return planted if hunks is canary.PLANTED_HUNKS else clean
    monkeypatch.setattr(canary, "review_diff", fake)
    return canary.run_canary(timeout_s=5)


def test_dense_emission_exact_tags_for_every_outcome(monkeypatch, emitted):
    cases = [
        (_reviewed(_finding(canary.PLANTED_LINE)), _reviewed(), "caught", "pass", 1.0, 1.0),
        (_reviewed(), _reviewed(_finding(3)), "missed", "false_positive", 0.0, 0.0),
        (LlmReviewResponse(kind="all_failed", error="x"), LlmReviewResponse(kind="parse_failed"),
         "error", "error", 0.0, 0.0),
    ]
    for planted, clean, p_out, c_out, p_val, c_val in cases:
        emitted.clear()
        _run(monkeypatch, planted, clean)
        by_case = {t["case"]: (v, t) for m, v, t in emitted if m == "grug.elder.canary"}
        assert len(emitted) == 2
        assert by_case["planted"][0] == p_val and by_case["planted"][1]["outcome"] == p_out
        assert by_case["clean"][0] == c_val and by_case["clean"][1]["outcome"] == c_out


def test_backend_and_model_tags_when_known_omitted_when_unknown(monkeypatch, emitted):
    _run(monkeypatch, _reviewed(_finding(canary.PLANTED_LINE)),
         LlmReviewResponse(kind="all_failed", error="x"))
    tags = {t["case"]: t for _, _, t in emitted}
    assert tags["planted"] == {
        "case": "planted", "outcome": "caught", "backend": "openrouter", "model": "m-1",
    }
    assert tags["clean"] == {"case": "clean", "outcome": "error"}


def test_emit_failure_on_one_case_does_not_hide_the_other(monkeypatch, caplog):
    seen = []

    def flaky(metric, value, tags=None):
        seen.append(tags["case"])
        if tags["case"] == "planted":
            raise OSError("udp")
    monkeypatch.setattr(canary, "emit_gauge", flaky)
    with caplog.at_level(logging.INFO):
        _run(monkeypatch, _reviewed(), _reviewed())
    assert seen == ["planted", "clean"]
    assert any(r.getMessage() == "elder_canary_emit_failed" and r.kind == "OSError" for r in caplog.records)
    assert sum(r.getMessage() == "elder_canary_result" for r in caplog.records) == 2


def test_logs_elder_canary_result(monkeypatch, emitted, caplog):
    with caplog.at_level(logging.INFO):
        _run(monkeypatch, _reviewed(_finding(canary.PLANTED_LINE)), _reviewed())
    recs = [r for r in caplog.records if r.getMessage() == "elder_canary_result"]
    assert {r.case for r in recs} == {"planted", "clean"}
    p = next(r for r in recs if r.case == "planted")
    assert (p.outcome, p.backend, p.model, p.findings_count) == ("caught", "openrouter", "m-1", 1)
    assert isinstance(p.elapsed_s, float)


def test_exception_in_review_is_error_and_still_emits_both(monkeypatch, emitted):
    def fake(hunks, installation_id, pr_context=None, **kw):
        if hunks is canary.PLANTED_HUNKS:
            raise RuntimeError("provider down")
        return _reviewed()
    monkeypatch.setattr(canary, "review_diff", fake)
    canary.run_canary(timeout_s=5)
    out = {t["case"]: (v, t["outcome"]) for _, v, t in emitted}
    assert out == {"planted": (0.0, "error"), "clean": (1.0, "pass")}


def test_timeout_emits_error_for_the_hung_case_and_cancels(monkeypatch, emitted):
    release = threading.Event()
    seen_cancel: list[threading.Event] = []

    def fake(hunks, installation_id, pr_context=None, cancel_event=None, **kw):
        if hunks is canary.PLANTED_HUNKS:
            seen_cancel.append(cancel_event)
            release.wait(5)
        return _reviewed()
    monkeypatch.setattr(canary, "review_diff", fake)
    canary.run_canary(timeout_s=0.2)
    release.set()
    out = {t["case"]: t["outcome"] for _, _, t in emitted}
    assert out == {"planted": "error", "clean": "pass"}
    assert seen_cancel[0] is not None and seen_cancel[0].is_set()


def test_never_posts_to_github_and_uses_fake_context(monkeypatch, emitted):
    captured = {}

    def fake(hunks, installation_id, pr_context=None, **kw):
        captured.update(iid=installation_id, ctx=pr_context)
        return _reviewed()
    monkeypatch.setattr(canary, "review_diff", fake)
    import github_app_auth
    monkeypatch.setattr(github_app_auth, "with_install_token_retry",
                        lambda *a, **k: pytest.fail("canary must not fetch a GitHub token"))
    canary.run_canary(timeout_s=5)
    assert captured["iid"] == 0
    assert captured["ctx"]["repo"] == canary.CANARY_REPO
    assert "head_sha" not in captured["ctx"] and "installation_id" not in captured["ctx"]


# ---- poller wiring -----------------------------------------------------

@pytest.mark.parametrize("minute,due", [(0, True), (14, True), (15, False), (30, False), (59, False)])
def test_hourly_gate_first_poller_slot(minute, due):
    now = datetime(2026, 10, 5, 7, minute, 3, tzinfo=timezone.utc)
    assert poller_handler._elder_canary_due(now) is due


def test_gate_respects_kill_switch(monkeypatch):
    monkeypatch.setenv("GRUG_ELDER_CANARY", "off")
    assert poller_handler._elder_canary_due(datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)) is False


def test_canary_pass_swallows_failure_and_logs_kind(monkeypatch, caplog):
    def boom(**kw):
        raise ValueError("x")
    monkeypatch.setattr(canary, "run_canary", boom)
    with caplog.at_level(logging.WARNING):
        poller_handler._elder_canary_pass()
    rec = next(r for r in caplog.records if r.getMessage() == "elder_canary_pass_failed")
    assert rec.kind == "ValueError"


def _stub_handler_deps(monkeypatch):
    monkeypatch.setattr(poller_handler, "list_allowlisted_installs", lambda: [])
    monkeypatch.setattr(poller_handler, "_replay_missed_deliveries", lambda: {})
    monkeypatch.setattr(poller_handler, "_prove_roles_anywhere_identity", lambda: None)
    monkeypatch.setattr(poller_handler, "_install_reconciliation_pass", lambda: (0, 0, 0))
    monkeypatch.setattr("check_run_reconciler.reconcile_installs", lambda installs: (0, 0))


def test_handler_runs_canary_when_due_and_after_replay_and_survives_failure(monkeypatch):
    _stub_handler_deps(monkeypatch)
    order = []
    monkeypatch.setattr(poller_handler, "_replay_missed_deliveries", lambda: order.append("replay") or {})
    monkeypatch.setattr(poller_handler, "_elder_canary_due", lambda now=None: True)

    def boom():
        order.append("canary")
        raise RuntimeError("never escapes")
    monkeypatch.setattr(poller_handler, "_elder_canary_pass", boom)
    result = poller_handler.handler({}, None)
    assert order == ["replay", "canary"]
    assert result["installs"] == 0


def test_handler_skips_canary_when_not_due(monkeypatch):
    _stub_handler_deps(monkeypatch)
    monkeypatch.setattr(poller_handler, "_elder_canary_due", lambda now=None: False)
    monkeypatch.setattr(poller_handler, "_elder_canary_pass",
                        lambda: pytest.fail("canary ran off-gate"))
    poller_handler.handler({}, None)
