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


def _only_tpm(persona: str) -> bool:
    """is_persona_enabled stub — keep TPM-only behavior for legacy
    test cases that predate the Elder persona (so they assert TPM-only
    response shapes)."""
    return persona == "tpm"


def test_pull_request_dispatches_when_allowlisted():
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", side_effect=lambda *a: _only_tpm(a[2])), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation") as _mock_pub:
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())
    assert out["status"] == "dispatched"
    assert len(out["personas"]) == 1
    assert out["personas"][0]["persona"] == "tpm"
    assert out["personas"][0]["result"] == "pass"
    mock_eval.assert_called_once()


def test_pull_request_no_op_when_all_personas_disabled():
    """Per-repo opt-out — all personas disabled short-circuits AFTER
    allowlist with `no_op`. Previously only TPM existed; now an opt-out
    must cover both."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=False), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation") as _mock_pub:
        out = dispatch("pull_request", _full_pr_payload())
    assert out["status"] == "no_op" and "all personas disabled" in out["reason"]
    mock_eval.assert_not_called()


def test_pull_request_fail_propagates():
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", side_effect=lambda *a: _only_tpm(a[2])), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation") as _mock_pub:
        mock_eval.return_value = type("R", (), {"passed": False})()
        out = dispatch("pull_request", _full_pr_payload())
    assert out["personas"][0]["result"] == "fail"


def test_pull_request_blocked_when_not_allowlisted():
    """Defense-in-depth: non-allowlisted installs no_op silently and
    NEVER reach the TPM evaluator (no GitHub API call, no check-run)."""
    with patch("dispatcher.is_install_allowlisted", return_value=False), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation") as _mock_pub:
        out = dispatch("pull_request", _full_pr_payload())
    assert out["status"] == "no_op" and "not allowlisted" in out["reason"]
    mock_eval.assert_not_called()


def test_pull_request_publish_failure_returns_skip_with_log_context():
    """Peer-review CRITICAL (4x): publish_tpm_evaluation exceptions must
    NOT propagate uncaught into main.py (which would 500 the webhook
    with no install/repo/PR coords in the error log). The dispatcher
    must catch HTTPStatusError + RequestError, log structured fields,
    and return a `{"status": "skip", "reason": "publish_failed"}` dict."""
    import httpx

    fake_response = httpx.Response(status_code=502, request=httpx.Request("POST", "https://api.github.com/repos/githumps/infra/check-runs"))
    publish_error = httpx.HTTPStatusError("502 Bad Gateway", request=fake_response.request, response=fake_response)

    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", side_effect=lambda *a: _only_tpm(a[2])), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation", side_effect=publish_error):
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    # Publish failure no longer short-circuits the whole dispatcher —
    # it's recorded per-persona so the other persona can still run.
    assert out["status"] == "dispatched"
    assert out["personas"][0] == {"persona": "tpm", "result": "publish_failed"}


def test_pull_request_publish_transport_error_returns_skip():
    """Same shape as above for transport-level errors (timeout, DNS, connection-reset)."""
    import httpx

    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", side_effect=lambda *a: _only_tpm(a[2])), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation",
               side_effect=httpx.ConnectTimeout("timed out", request=None)):
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    assert out["personas"][0]["result"] == "publish_failed"


def test_pull_request_dispatches_both_personas_independently():
    """Acceptance criterion (#185): TPM and Elder run on the same event,
    producing independent verdicts. Both must appear in the results
    list. Order: TPM first, Elder second."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation"), \
         patch("async_dispatch.enqueue_elder_review", return_value=True) as mock_enq:
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    assert out["status"] == "dispatched"
    assert len(out["personas"]) == 2
    assert out["personas"][0]["persona"] == "tpm"
    # Elder is now OFFLOADED (#272): the sync path enqueues, not runs.
    assert out["personas"][1] == {"persona": "code_reviewer", "result": "queued"}
    mock_enq.assert_called_once()


def test_pull_request_tpm_failure_does_not_skip_elder_enqueue():
    """One persona failing must not skip the other — independence is
    the load-bearing property. With #272, "Elder runs" means "Elder is
    enqueued": a TPM publish failure must not stop the self-invoke."""
    import httpx

    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation",
               side_effect=httpx.ConnectError("dns down")), \
         patch("async_dispatch.enqueue_elder_review", return_value=True) as mock_enq:
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    # TPM publish failed → recorded but did not skip the dispatcher.
    assert out["personas"][0] == {"persona": "tpm", "result": "publish_failed"}
    # Elder still enqueued.
    mock_enq.assert_called_once()
    assert out["personas"][1] == {"persona": "code_reviewer", "result": "queued"}


def test_pull_request_tpm_evaluator_exception_does_not_skip_elder_enqueue():
    """An unhandled exception in `evaluate_pull_request` (TPM evaluator
    bug) must not propagate up `_handle_pull_request` and skip the Elder
    enqueue. The broad final guard in personas/tpm/webhook_dispatch.py
    catches it (moved from the old dispatcher._dispatch_tpm, #465)."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch("personas.tpm.persona.evaluate_pull_request",
               side_effect=RuntimeError("evaluator regression")), \
         patch("async_dispatch.enqueue_elder_review", return_value=True) as mock_enq:
        out = dispatch("pull_request", _full_pr_payload())

    # TPM unhandled — but Elder still enqueued.
    assert out["personas"][0] == {"persona": "tpm", "result": "unhandled_error"}
    mock_enq.assert_called_once()
    assert out["personas"][1] == {"persona": "code_reviewer", "result": "queued"}


def test_pull_request_elder_enqueue_failure_does_not_skip_tpm_status():
    """Inverse: a failed Elder ENQUEUE (rare Lambda throttle) must not
    corrupt the TPM result. Elder's status becomes `enqueue_failed`; the
    sync path does NOT fall back to a synchronous Elder run (that would
    re-block the <10s ACK guarantee, #272)."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation"), \
         patch("async_dispatch.enqueue_elder_review", return_value=False):
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    assert out["personas"][0]["result"] == "pass"  # TPM unaffected
    assert out["personas"][1] == {
        "persona": "code_reviewer", "result": "enqueue_failed",
    }


def test_pull_request_threads_delivery_id_to_enqueue():
    """The X-GitHub-Delivery id must reach the enqueue so the async
    worker can key its idempotency claim on it (#272)."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": True}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation"), \
         patch("async_dispatch.enqueue_elder_review", return_value=True) as mock_enq:
        mock_eval.return_value = type("R", (), {"passed": True})()
        dispatch("pull_request", _full_pr_payload(), delivery_id="deliv-abc")

    _, kwargs = mock_enq.call_args
    assert kwargs["delivery_id"] == "deliv-abc"
    assert kwargs["blocking"] is True  # plumbed from code_reviewer_blocking


def test_pull_request_missing_repo_id_skips_elder_but_runs_tpm():
    """Asymmetric contract documented in `_handle_pull_request`:
    missing `repo_id` (payload-shape glitch) → TPM dispatches anyway
    (legacy enabled-by-default), Elder skips with reason=no_repo_id
    since it can't call is_persona_enabled without a repo_id. A
    refactor unifying the two branches would silently flip Elder to
    enabled-by-default. This test pins the asymmetry."""
    payload = _full_pr_payload()
    payload["repository"].pop("id", None)

    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation"), \
         patch("async_dispatch.enqueue_elder_review") as mock_enq:
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", payload)

    assert len(out["personas"]) == 1
    assert out["personas"][0]["persona"] == "tpm"
    mock_enq.assert_not_called()


def test_pull_request_code_reviewer_disabled_skips_only_elder():
    """When `code_reviewer_enabled=False`, the Elder dispatch is skipped
    but TPM still runs (and vice versa)."""
    def _only_tpm_enabled(install_id, repo_id, persona):
        return persona == "tpm"

    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", side_effect=_only_tpm_enabled), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation"), \
         patch("async_dispatch.enqueue_elder_review") as mock_enq:
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    assert len(out["personas"]) == 1
    assert out["personas"][0]["persona"] == "tpm"
    mock_enq.assert_not_called()


def test_installation_created_records_row():
    payload = {
        "action": "created",
        "installation": {
            "id": 555,
            "account": {"login": "githumps", "type": "User", "id": 100},
        },
        "sender": {"id": 100, "login": "githumps"},
    }
    with patch("dispatcher.record_installation") as mock_rec, \
         patch("dispatcher.is_install_allowlisted", return_value=False):
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
    with patch("dispatcher.record_installation") as mock_rec, \
         patch("dispatcher.is_install_allowlisted", return_value=False):
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


# ── repository_ruleset (self-healing) ───────────────────────────────


def _ruleset_deleted_payload(
    *,
    ruleset_name: str = "Grug — TPM Enforcement",
    ruleset_id: int = 42,
    install_id: int = 999,
    repo_id: int = 7777,
    repo_full_name: str = "githumps/infra",
    default_branch: str = "main",
):
    return {
        "action": "deleted",
        "repository_ruleset": {"id": ruleset_id, "name": ruleset_name},
        "repository": {
            "id": repo_id,
            "name": repo_full_name.split("/")[1],
            "full_name": repo_full_name,
            "owner": {"login": repo_full_name.split("/")[0]},
            "default_branch": default_branch,
        },
        "installation": {"id": install_id},
    }


def test_repository_ruleset_non_delete_action_noop():
    out = dispatch("repository_ruleset", {"action": "created", "repository_ruleset": {"id": 1, "name": "x"}})
    assert out["status"] == "no_op"


def test_repository_ruleset_non_grug_ruleset_noop():
    payload = _ruleset_deleted_payload(ruleset_name="CI Required")
    out = dispatch("repository_ruleset", payload)
    assert out["status"] == "no_op" and "not grug-managed" in out["reason"]


def test_repository_ruleset_heals_when_tpm_enabled():
    payload = _ruleset_deleted_payload()
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={
             "tpm_enabled": True, "enforcement_ruleset_id": 42,
             "force_disable_enforcement": False,
         }), \
         patch("dispatcher._heal_enforcement_on_repo") as mock_heal:
        out = dispatch("repository_ruleset", payload)
    assert out["status"] == "healed"
    mock_heal.assert_called_once()


def test_repository_ruleset_skips_when_not_allowlisted():
    payload = _ruleset_deleted_payload()
    with patch("dispatcher.is_install_allowlisted", return_value=False):
        out = dispatch("repository_ruleset", payload)
    assert out["status"] == "no_op" and "not allowlisted" in out["reason"]


def test_repository_ruleset_skips_when_tpm_disabled():
    payload = _ruleset_deleted_payload()
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=False):
        out = dispatch("repository_ruleset", payload)
    assert out["status"] == "no_op" and "tpm disabled" in out["reason"]


def test_repository_ruleset_skips_when_force_disable():
    payload = _ruleset_deleted_payload()
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={
             "tpm_enabled": True, "enforcement_ruleset_id": 42,
             "force_disable_enforcement": True,
         }):
        out = dispatch("repository_ruleset", payload)
    assert out["status"] == "no_op" and "force_disable" in out["reason"]
