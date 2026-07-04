"""Smasher dispatch orchestration tests (#469).

Locks the wiring around the sandbox: global kill switch, degrade paths, and the
survived-mutant -> advisory-finding -> publish flow. The GitHub + sandbox calls
are patched; no network, no cluster.
"""

from __future__ import annotations

import secrets_loader
from personas.smasher import dispatch as sm_dispatch
from personas.smasher import trial_runner
from personas.smasher.sandbox import SurvivedMutant, TrialResult


def _payload(action="opened"):
    return {
        "action": action,
        "pull_request": {
            "number": 7,
            "head": {"sha": "deadbeefcafe"},
            "body": "closes #1",
        },
        "repository": {"owner": {"login": "acme"}, "name": "widget", "id": 99},
        "installation": {"id": 123},
    }


_DIFF = (
    "diff --git a/pkg/mod.py b/pkg/mod.py\n"
    "--- a/pkg/mod.py\n"
    "+++ b/pkg/mod.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def f(x):\n"
    "+    y = x > 0\n"
    "     return x\n"
)


def _wire(monkeypatch, *, diff=_DIFF, trial=None, enabled=True):
    """Patch the GitHub + sandbox seams. Returns a dict capturing published
    check-runs so a test can assert on them."""
    captured: dict = {"checks": [], "reviews": [], "verdicts": []}

    monkeypatch.setattr(secrets_loader, "get_smasher_enabled", lambda: enabled)
    monkeypatch.setattr(secrets_loader, "get_smasher_network_policy_enforced", lambda: True)
    monkeypatch.setattr(
        sm_dispatch, "with_install_token_retry",
        lambda _iid, fn: fn("tok"),
    )
    monkeypatch.setattr(sm_dispatch, "_fetch_pr_diff", lambda *a, **k: diff)
    monkeypatch.setattr(sm_dispatch, "get_scoped_install_token", lambda *a, **k: "scoped")
    monkeypatch.setattr(
        sm_dispatch, "post_check_run",
        lambda token, owner, repo, result, **k: captured["checks"].append(result),
    )
    monkeypatch.setattr(
        sm_dispatch, "post_review",
        lambda token, owner, repo, **k: captured["reviews"].append(k),
    )
    monkeypatch.setattr(
        sm_dispatch, "record_check_verdict",
        lambda **k: captured["verdicts"].append(k),
    )
    if trial is not None:
        monkeypatch.setattr(trial_runner, "launch_trial", lambda **k: trial)
    return captured


def test_globally_disabled_short_circuits(monkeypatch):
    captured = _wire(monkeypatch, enabled=False)
    out = sm_dispatch.dispatch_smasher_review(_payload(), blocking=False)
    assert out["result"] == "disabled_global"
    # No check-run posted when the master switch is off.
    assert captured["checks"] == []


def test_refuses_to_run_when_netpol_not_affirmed(monkeypatch):
    # Fail-closed egress gate: even with the master switch ON, Smasher must NOT
    # run author code unless the operator affirmed a policy-enforcing CNI.
    captured = _wire(monkeypatch, enabled=True)
    monkeypatch.setattr(secrets_loader, "get_smasher_network_policy_enforced", lambda: False)
    out = sm_dispatch.dispatch_smasher_review(_payload(), blocking=False)
    assert out["result"] == "disabled_no_netpol_enforcement"
    assert captured["checks"] == []  # no Job launched, no check posted


def test_survived_mutant_becomes_advisory_finding(monkeypatch):
    trial = TrialResult(
        status="completed", total=2, killed=1,
        survived=(SurvivedMutant("pkg/mod.py", 2, "comparison-flip", ">", ">="),),
    )
    captured = _wire(monkeypatch, trial=trial)
    out = sm_dispatch.dispatch_smasher_review(_payload(), blocking=False)
    assert out["persona"] == "smasher"
    # A check-run was published, advisory (neutral) with the finding surfaced.
    assert captured["checks"], "expected a check-run"
    check = captured["checks"][0]
    assert check.conclusion == "neutral"  # advisory-only, never blocks
    assert "smasher" in captured["verdicts"][0]["persona_key"]
    assert captured["verdicts"][0]["findings_count"] == 1


def test_clean_trial_publishes_pass(monkeypatch):
    trial = TrialResult(status="completed", total=3, killed=3, survived=())
    captured = _wire(monkeypatch, trial=trial)
    out = sm_dispatch.dispatch_smasher_review(_payload(), blocking=False)
    assert out["result"] == "pass"
    assert captured["verdicts"][0]["findings_count"] == 0


def test_no_python_changes_is_clean_pass(monkeypatch):
    non_py = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1,2 @@\n hi\n+more\n"
    )
    captured = _wire(monkeypatch, diff=non_py, trial=None)
    out = sm_dispatch.dispatch_smasher_review(_payload(), blocking=False)
    # No targets -> no Job launched -> clean advisory pass.
    assert out["result"] == "pass"
    assert captured["checks"]


def test_degraded_trial_surfaces_specific_reason(monkeypatch):
    trial = TrialResult(status="degraded", total=0, killed=0, survived=(),
                        reason="baseline_failed")
    captured = _wire(monkeypatch, trial=trial)
    out = sm_dispatch.dispatch_smasher_review(_payload(), blocking=False)
    assert out["result"] == "degraded"
    # The specific cause is surfaced on the check, not a generic "degraded".
    assert captured["verdicts"][0]["degraded_reason"] == "baseline_failed"


def test_completed_but_zero_mutants_degrades_not_passes(monkeypatch):
    # dispatch guaranteed non-empty targets, so a completed Trial with total=0
    # means the checkout/targets broke inside the Job -> degrade, never a green
    # "tests strong" pass (ADR-0003 "no lies").
    trial = TrialResult(status="completed", total=0, killed=0, survived=())
    captured = _wire(monkeypatch, trial=trial)
    out = sm_dispatch.dispatch_smasher_review(_payload(), blocking=False)
    assert out["result"] == "degraded"
    assert captured["verdicts"][0]["findings_count"] == 0


def test_scoped_token_minted_contents_read_single_repo(monkeypatch):
    # ADR-0013 security property: the sandbox token is down-scoped to
    # contents:read on the ONE repo — assert the mint args, not just that it's
    # called (a full-scope token would pass a lambda that ignores args).
    trial = TrialResult(status="completed", total=2, killed=2, survived=())
    _wire(monkeypatch, trial=trial)
    seen = {}
    monkeypatch.setattr(
        sm_dispatch, "get_scoped_install_token",
        lambda iid, *, repositories, permissions: seen.update(
            repos=repositories, perms=permissions) or "scoped",
    )
    sm_dispatch.dispatch_smasher_review(_payload(), blocking=False)
    assert seen["repos"] == ["widget"]
    assert seen["perms"] == {"contents": "read"}


def test_diff_fetch_failure_degrades(monkeypatch):
    import httpx

    captured = _wire(monkeypatch, trial=None)

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(sm_dispatch, "with_install_token_retry", lambda _iid, fn: boom())
    out = sm_dispatch.dispatch_smasher_review(_payload(), blocking=False)
    assert out["result"] == "degraded"
    assert captured["verdicts"][0]["degraded_reason"] == "fetch_or_parse_failed"
