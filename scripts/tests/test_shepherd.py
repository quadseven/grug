import pytest

# tests for shepherd deployment verification and chain escalation
from pathlib import Path
import types

# Import the module
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
from shepherd import (
    StageVerdict,
    check_deployment,
    deployment_stage_verdicts,
    evaluate_chain,
    escalate_once,
    get_latest_digest,
    get_latest_push_digest,
)


def test_merged_but_not_deployed_is_a_distinct_loud_state(monkeypatch, capsys):
    import shepherd

    monkeypatch.setattr(shepherd, "get_latest_digest", lambda service: ("old", "old-digest"))
    monkeypatch.setattr(shepherd, "get_latest_push_digest", lambda service: ("new", "new-digest"))

    assert shepherd.deployment_status("service") == "merged-but-not-deployed"
    assert shepherd.main("service") == 1
    assert "MERGED-BUT-NOT-DEPLOYED" in capsys.readouterr().err

# Helper to patch functions

def make_patch(set_digest, set_push):
    def patched_get_latest_digest(service):
        return set_digest
    def patched_get_latest_push_digest(service):
        return set_push
    return patched_get_latest_digest, patched_get_latest_push_digest


# Test when digests match

def test_check_deployment_true(monkeypatch):
    patch_digest, patch_push = make_patch(("tag1", "abc123"), ("tag2", "abc123"))
    monkeypatch.setattr("shepherd.get_latest_digest", patch_digest)
    monkeypatch.setattr("shepherd.get_latest_push_digest", patch_push)
    assert check_deployment("service") is True

# Test when digests differ

def test_check_deployment_false_on_diff(monkeypatch):
    patch_digest, patch_push = make_patch(("deployed", "diff1"), ("pushed", "diff2"))
    monkeypatch.setattr("shepherd.get_latest_digest", patch_digest)
    monkeypatch.setattr("shepherd.get_latest_push_digest", patch_push)
    assert check_deployment("service") is False

# Test when pushed digest missing

def test_check_deployment_false_on_missing_push(monkeypatch):
    patch_digest, patch_push = make_patch(("deployed", "abc123"), ("tag", ""))
    monkeypatch.setattr("shepherd.get_latest_digest", patch_digest)
    monkeypatch.setattr("shepherd.get_latest_push_digest", patch_push)
    assert check_deployment("service") is False

# Test when deployed digest missing

def test_check_deployment_false_on_missing_deploy(monkeypatch):
    patch_digest, patch_push = make_patch(("", ""), ("tag", "abc123"))
    monkeypatch.setattr("shepherd.get_latest_digest", patch_digest)
    monkeypatch.setattr("shepherd.get_latest_push_digest", patch_push)
    assert check_deployment("service") is False

# Test that get_latest_push_digest is used (implicit via patch)

def test_check_deployment_calls(monkeypatch):
    called = {"push": False, "deploy": False}
    def fake_push(service):
        called["push"] = True
        return ("tag", "abc")
    def fake_deploy(service):
        called["deploy"] = True
        return ("tag", "abc")
    monkeypatch.setattr("shepherd.get_latest_push_digest", fake_push)
    monkeypatch.setattr("shepherd.get_latest_digest", fake_deploy)
    assert check_deployment("svc") == True
    assert called["push"] and called["deploy"]

# ---- chain escalation (T832) ----


def _broken(name: str = "build", evidence: str = "build failed") -> StageVerdict:
    return StageVerdict(name, False, evidence)


def _ok(name: str = "build", evidence: str = "digest matches") -> StageVerdict:
    return StageVerdict(name, True, evidence)


def test_broken_chain_notifies_once_names_stage_and_evidence(tmp_path):
    state = tmp_path / "state.json"
    notified = []

    def notify(message, stage, evidence):
        notified.append((message, stage, evidence))

    check = lambda: _broken("build", "exit code 1: build failed")

    first = escalate_once([("build", check)], state_path=str(state), notify=notify)
    assert first == "broken"
    assert len(notified) == 1
    message, stage, evidence = notified[0]
    assert stage == "build"
    assert "build failed" in evidence
    assert "broken" in message

    # a second run of the same break must not notify again
    second = escalate_once([("build", check)], state_path=str(state), notify=notify)
    assert second == "broken"
    assert len(notified) == 1


def test_recovery_is_reported_so_a_stale_alarm_cannot_linger(tmp_path):
    state = tmp_path / "state.json"
    notified = []

    def notify(message, stage, evidence):
        notified.append((message, stage, evidence))

    assert escalate_once([("build", _broken)], state_path=str(state), notify=notify) == "broken"
    assert len(notified) == 1

    assert escalate_once([("build", _ok)], state_path=str(state), notify=notify) == "recovered"
    assert len(notified) == 2
    message, stage, evidence = notified[1]
    assert stage is None
    assert "recovered" in message
    assert "build" in evidence


def test_healthy_chain_notifies_nothing(tmp_path):
    state = tmp_path / "state.json"
    notified = []

    def notify(message, stage, evidence):
        notified.append((message, stage, evidence))

    assert escalate_once([("build", _ok)], state_path=str(state), notify=notify) == "healthy"
    assert notified == []

    # still healthy, still nothing
    assert escalate_once([("build", _ok)], state_path=str(state), notify=notify) == "healthy"
    assert notified == []


def test_cancelled_counts_as_broken_only_success_is_success(tmp_path):
    state = tmp_path / "state.json"
    notified = []

    def notify(message, stage, evidence):
        notified.append((message, stage, evidence))

    cancelled = lambda: _broken("schedule", "scheduled run cancelled")

    assert escalate_once([("schedule", cancelled)], state_path=str(state), notify=notify) == "broken"
    assert len(notified) == 1
    assert notified[0][1] == "schedule"
    assert "cancelled" in notified[0][2]


def test_evaluate_chain_returns_first_broken_stage_and_short_circuits():
    calls = []

    def first():
        calls.append("first")
        return _broken("build", "build failed")

    def second():
        calls.append("second")
        return _broken("deploy", "digest mismatch")

    verdict = evaluate_chain([("build", first), ("deploy", second)])
    assert verdict is not None
    assert verdict.name == "build"
    assert verdict.ok is False
    assert "build failed" in verdict.evidence
    assert calls == ["first"]

    assert evaluate_chain([("build", _ok)]) is None


def test_a_different_stage_after_recovery_notifies_again(tmp_path):
    state = tmp_path / "state.json"
    notified = []

    def notify(message, stage, evidence):
        notified.append((message, stage, evidence))

    assert escalate_once([("build", _broken)], state_path=str(state), notify=notify) == "broken"
    assert escalate_once([("build", _ok)], state_path=str(state), notify=notify) == "recovered"

    assert escalate_once([("deploy", lambda: _broken("deploy", "digest mismatch"))], state_path=str(state), notify=notify) == "broken"
    assert len(notified) == 3
    assert notified[2][1] == "deploy"


def test_deployment_stage_verdicts_names_the_broken_link(monkeypatch):
    import shepherd

    monkeypatch.setattr(shepherd, "get_latest_digest", lambda service: ("tag", ""))
    monkeypatch.setattr(shepherd, "get_latest_push_digest", lambda service: ("tag", "abc"))
    verdicts = shepherd.deployment_stage_verdicts("svc")
    assert len(verdicts) == 1
    assert verdicts[0].name == "deploy"
    assert verdicts[0].ok is False
    assert "cancelled" in verdicts[0].evidence

    monkeypatch.setattr(shepherd, "get_latest_digest", lambda service: ("tag", "abc"))
    monkeypatch.setattr(shepherd, "get_latest_push_digest", lambda service: ("tag", "def"))
    verdicts = shepherd.deployment_stage_verdicts("svc")
    assert verdicts[0].ok is False
    assert "merged-but-not-deployed" in verdicts[0].evidence

    monkeypatch.setattr(shepherd, "get_latest_digest", lambda service: ("tag", "abc"))
    monkeypatch.setattr(shepherd, "get_latest_push_digest", lambda service: ("tag", "abc"))
    verdicts = shepherd.deployment_stage_verdicts("svc")
    assert verdicts[0].ok is True

    monkeypatch.setattr(shepherd, "get_latest_digest", lambda service: ("tag", ""))
    monkeypatch.setattr(shepherd, "get_latest_push_digest", lambda service: ("tag", ""))
    verdicts = shepherd.deployment_stage_verdicts("svc")
    assert verdicts[0].name == "reference"
    assert verdicts[0].ok is False
    assert "reference never bumped" in verdicts[0].evidence

