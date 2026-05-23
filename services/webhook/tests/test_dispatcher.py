"""Tests for webhook → persona dispatcher.

Covers routing decisions, payload-shape gates, allowlist gating, and
installation event handling. TPM evaluator + install_store are
patched.
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


def test_installation_repositories_no_op():
    out = dispatch("installation_repositories", {})
    assert out["status"] == "no_op"


def test_pull_request_unhandled_action_skips():
    payload = {"action": "labeled", "pull_request": {}, "repository": {}}
    out = dispatch("pull_request", payload)
    assert out["status"] == "no_op" and "labeled" in out["reason"]


def test_pull_request_incomplete_payload_skips():
    payload = {"action": "opened", "pull_request": {}, "repository": {}, "installation": {}}
    out = dispatch("pull_request", payload)
    assert out["status"] == "skip" and out["reason"] == "incomplete_payload"


def _full_pr_payload():
    return {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "body": "## Why\nbecause we need it badly\n## Acceptance criteria\n- a\n- b\n- c\n## Out of scope\nx\nSize: S\ncloses #1",
            "head": {"sha": "abc123def456"},
        },
        "repository": {"id": 7777, "name": "infra", "owner": {"login": "githumps"}, "full_name": "githumps/infra"},
        "installation": {"id": 999},
    }


def test_pull_request_dispatches_when_allowlisted():
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation") as _mock_pub:
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())
    assert out["status"] == "dispatched"
    assert out["persona"] == "tpm"
    assert out["result"] == "pass"
    mock_eval.assert_called_once()


def test_pull_request_blocked_when_tpm_disabled_for_repo():
    """Slice 7 #28 — per-repo opt-out short-circuits AFTER allowlist."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=False), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation") as _mock_pub:
        out = dispatch("pull_request", _full_pr_payload())
    assert out["status"] == "no_op" and "tpm disabled" in out["reason"]
    mock_eval.assert_not_called()


def test_pull_request_fail_propagates():
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation") as _mock_pub:
        mock_eval.return_value = type("R", (), {"passed": False})()
        out = dispatch("pull_request", _full_pr_payload())
    assert out["result"] == "fail"


def test_pull_request_blocked_when_not_allowlisted():
    """Defense-in-depth: non-allowlisted installs no_op silently and
    NEVER reach the TPM evaluator (no GitHub API call, no check-run)."""
    with patch("dispatcher.is_install_allowlisted", return_value=False), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation") as _mock_pub:
        out = dispatch("pull_request", _full_pr_payload())
    assert out["status"] == "no_op" and "not allowlisted" in out["reason"]
    mock_eval.assert_not_called()


def test_installation_created_records_row():
    payload = {
        "action": "created",
        "installation": {
            "id": 555,
            "account": {"login": "githumps", "type": "User", "id": 100},
        },
        "sender": {"id": 100, "login": "githumps"},
    }
    with patch("dispatcher.record_installation") as mock_rec:
        out = dispatch("installation", payload)
    assert out["status"] == "recorded" and out["action"] == "created"
    mock_rec.assert_called_once_with(
        install_id=555, account_login="githumps", account_type="User",
        installed_by_user_id=100,
    )


def test_installation_created_org_uses_sender_id():
    """Org installs: installed_by must be the human sender, not the org."""
    payload = {
        "action": "created",
        "installation": {
            "id": 555,
            "account": {"login": "acme-org", "type": "Organization", "id": 9},
        },
        "sender": {"id": 100, "login": "evan"},
    }
    with patch("dispatcher.record_installation") as mock_rec:
        dispatch("installation", payload)
    assert mock_rec.call_args.kwargs["installed_by_user_id"] == 100


def test_installation_deleted_removes_row():
    payload = {"action": "deleted", "installation": {"id": 555}}
    with patch("dispatcher.delete_installation") as mock_del:
        out = dispatch("installation", payload)
    assert out["status"] == "recorded" and out["action"] == "deleted"
    mock_del.assert_called_once_with(555)


def test_installation_no_id_skips():
    out = dispatch("installation", {"action": "created", "installation": {}})
    assert out["status"] == "skip"


def test_installation_unhandled_action():
    out = dispatch("installation", {"action": "suspend", "installation": {"id": 1}})
    assert out["status"] == "no_op" and "suspend" in out["reason"]


# Codex post-review #51 — preserve installer on perm-accept / unsuspend


def test_new_permissions_accepted_preserves_existing_installer():
    payload = {
        "action": "new_permissions_accepted",
        "installation": {
            "id": 555,
            "account": {"login": "acme-org", "type": "Organization", "id": 9},
        },
        "sender": {"id": 999, "login": "different-admin"},  # NOT original installer
    }
    with patch("dispatcher.get_installation", return_value={"PK": "INST#555"}), \
         patch("dispatcher.record_installation") as mock_rec:
        out = dispatch("installation", payload)
    assert out["status"] == "no_op" and "preserved" in out["reason"]
    mock_rec.assert_not_called()


def test_unsuspend_preserves_existing_installer():
    payload = {
        "action": "unsuspend",
        "installation": {"id": 555, "account": {"login": "acme", "type": "User", "id": 1}},
        "sender": {"id": 999, "login": "another-user"},
    }
    with patch("dispatcher.get_installation", return_value={"PK": "INST#555"}), \
         patch("dispatcher.record_installation") as mock_rec:
        out = dispatch("installation", payload)
    assert out["status"] == "no_op"
    mock_rec.assert_not_called()


def test_new_permissions_accepted_backfills_when_no_existing_row():
    """Edge case: missed the `created` event somehow → record now."""
    payload = {
        "action": "new_permissions_accepted",
        "installation": {"id": 555, "account": {"login": "evan", "type": "User", "id": 100}},
        "sender": {"id": 100, "login": "evan"},
    }
    with patch("dispatcher.get_installation", return_value=None), \
         patch("dispatcher.record_installation") as mock_rec:
        out = dispatch("installation", payload)
    assert out["status"] == "recorded" and "backfill" in out["action"]
    mock_rec.assert_called_once()
