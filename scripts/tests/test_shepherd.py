import pytest

# tests for shepherd deployment verification
from pathlib import Path
import types

# Import the module
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
from shepherd import check_deployment, get_latest_digest, get_latest_push_digest

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

