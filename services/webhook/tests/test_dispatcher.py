"""Tests for webhook → persona dispatcher.

Covers routing decisions and payload-shape gates without invoking the
real GitHub API (TPM evaluator is patched).
"""

from __future__ import annotations

from unittest.mock import patch

import personas.tpm.persona  # noqa: F401 — register submodule for patch path
from dispatcher import dispatch


def test_unknown_event_no_op():
    out = dispatch("issues", {})
    assert out["status"] == "no_op"


def test_pull_request_review_placeholder():
    out = dispatch("pull_request_review", {})
    assert out["status"] == "no_op" and "code-reviewer" not in out["reason"]


def test_pull_request_unhandled_action_skips():
    payload = {"action": "labeled", "pull_request": {}, "repository": {}}
    out = dispatch("pull_request", payload)
    assert out["status"] == "no_op" and "labeled" in out["reason"]


def test_pull_request_incomplete_payload_skips():
    payload = {"action": "opened", "pull_request": {}, "repository": {}, "installation": {}}
    out = dispatch("pull_request", payload)
    assert out["status"] == "skip" and out["reason"] == "incomplete_payload"


def _full_payload():
    return {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "body": "## Why\nbecause we need it badly\n## Acceptance criteria\n- a\n- b\n- c\n## Out of scope\nx\nSize: S\ncloses #1",
            "head": {"sha": "abc123def456"},
        },
        "repository": {"name": "infra", "owner": {"login": "githumps"}, "full_name": "githumps/infra"},
        "installation": {"id": 999},
    }


def test_pull_request_dispatches_to_tpm():
    with patch("personas.tpm.persona.evaluate_pull_request") as mock_eval:
        mock_result = type("R", (), {"passed": True})()
        mock_eval.return_value = mock_result
        out = dispatch("pull_request", _full_payload())
        assert out["status"] == "dispatched"
        assert out["persona"] == "tpm"
        assert out["result"] == "pass"
        mock_eval.assert_called_once()
        kwargs = mock_eval.call_args.kwargs
        assert kwargs["installation_id"] == 999
        assert kwargs["owner"] == "githumps"
        assert kwargs["repo"] == "infra"
        assert kwargs["pr_number"] == 42


def test_pull_request_fail_propagates():
    with patch("personas.tpm.persona.evaluate_pull_request") as mock_eval:
        mock_eval.return_value = type("R", (), {"passed": False})()
        out = dispatch("pull_request", _full_payload())
        assert out["result"] == "fail"
