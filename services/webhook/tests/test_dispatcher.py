"""Tests for webhook → persona dispatcher.

Covers routing decisions, payload-shape gates, allowlist gating, and
installation event handling. TPM evaluator + install_store are
patched.
"""

from __future__ import annotations

from unittest.mock import patch

import personas.tpm.persona  # noqa: F401 — register submodule for patch path
import pytest
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
        "repository": {"id": 7777, "name": "infra", "owner": {"login": "quadseven"}, "full_name": "quadseven/infra"},
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
        _mock_pub.return_value = {"persona": "tpm", "result": "pass"}
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
        _mock_pub.return_value = {"persona": "tpm", "result": "fail"}
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


def test_pull_request_publish_failure_surfaces_sentinel():
    """Since #550, publish_tpm_evaluation never raises on a failed
    publish — the shared seam classifies the failure (any exception in
    the token/POST chain, not just httpx shapes), records the honest
    errored Activity row, and returns the "publish_failed" sentinel.
    The dispatch must surface it per-persona without short-circuiting."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", side_effect=lambda *a: _only_tpm(a[2])), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation",
               return_value={"persona": "tpm", "result": "publish_failed"}):
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    # Publish failure no longer short-circuits the whole dispatcher —
    # it's recorded per-persona so the other persona can still run.
    assert out["status"] == "dispatched"
    assert out["personas"][0] == {"persona": "tpm", "result": "publish_failed"}


def test_pull_request_publish_failure_skips_ticket_compliance():
    """Pre-#550 a failed publish raised past the ticket-compliance
    advisory block, so the advisory never ran on that path. The seam
    migration must preserve that flow: publish_failed -> return
    immediately, run_ticket_compliance never called."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", side_effect=lambda *a: _only_tpm(a[2])), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation",
               return_value={"persona": "tpm", "result": "publish_failed"}), \
         patch("personas.tpm.ticket_compliance_run.run_ticket_compliance") as mock_compliance:
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    assert out["personas"][0] == {"persona": "tpm", "result": "publish_failed"}
    mock_compliance.assert_not_called()


def test_pull_request_publish_unexpected_raise_hits_final_guard():
    """The httpx-shaped catch around publish is gone (#550) — an
    UNEXPECTED raise from publish_tpm_evaluation (a bug, not a publish
    failure: those are classified inside the seam) must still be
    contained by the final guard, not propagate into main.py."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", side_effect=lambda *a: _only_tpm(a[2])), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation",
               side_effect=RuntimeError("seam contract bug")):
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    assert out["status"] == "dispatched"
    assert out["personas"][0] == {"persona": "tpm", "result": "unhandled_error"}


def test_pull_request_dispatches_all_personas_independently():
    """Acceptance criterion (#185): the personas run on the same event,
    producing independent verdicts. All must appear in the results
    list. Order: TPM first, Elder second, Guard third (#466), Smasher
    fourth (#469), Teller fifth (#554) - Warder/Pulse are filtered by
    `actions`/`events`, not by this test's universal enable patch."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation",
               return_value={"persona": "tpm", "result": "pass"}), \
         patch("async_dispatch.enqueue_elder_review", return_value=True) as mock_enq, \
         patch("async_dispatch.enqueue_guard_review", return_value=True) as mock_guard_enq, \
         patch("async_dispatch.enqueue_smasher_review", return_value=True) as mock_smasher_enq, \
         patch("async_dispatch.enqueue_walkthrough_review", return_value=True) as mock_teller_enq:
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    assert out["status"] == "dispatched"
    assert len(out["personas"]) == 5
    assert out["personas"][0]["persona"] == "tpm"
    # Elder + Guard + Smasher + Teller are OFFLOADED (#272/#466/#469/#554): the sync path enqueues.
    assert out["personas"][1] == {"persona": "code_reviewer", "result": "queued"}
    assert out["personas"][2] == {"persona": "guard", "result": "queued"}
    assert out["personas"][3] == {"persona": "smasher", "result": "queued"}
    assert out["personas"][4] == {"persona": "walkthrough", "result": "queued"}
    mock_enq.assert_called_once()
    mock_guard_enq.assert_called_once()
    mock_teller_enq.assert_called_once()
    mock_smasher_enq.assert_called_once()


def test_pull_request_tpm_failure_does_not_skip_elder_enqueue():
    """One persona failing must not skip the other — independence is
    the load-bearing property. With #272, "Elder runs" means "Elder is
    enqueued": a TPM publish failure must not stop the self-invoke."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation",
               return_value={"persona": "tpm", "result": "publish_failed"}), \
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
         patch("personas.tpm.persona.publish_tpm_evaluation",
               return_value={"persona": "tpm", "result": "pass"}), \
         patch("async_dispatch.enqueue_elder_review", return_value=False):
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    assert out["personas"][0]["result"] == "pass"  # TPM unaffected
    assert out["personas"][1] == {
        "persona": "code_reviewer", "result": "enqueue_failed",
    }


def test_pull_request_durable_enqueue_error_propagates_for_retry():
    """A failed durable handoff must become a non-2xx webhook response."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation") as mock_pub, \
         patch("async_dispatch.enqueue_elder_review",
               side_effect=RuntimeError("durable queue unavailable")), \
         patch("async_dispatch.enqueue_guard_review", return_value=True), \
         patch("async_dispatch.enqueue_smasher_review", return_value=True), \
         patch("async_dispatch.enqueue_walkthrough_review", return_value=True):
        mock_eval.return_value = type("R", (), {"passed": True})()
        mock_pub.return_value = {"persona": "tpm", "result": "pass"}
        with pytest.raises(RuntimeError, match="durable queue unavailable"):
            dispatch("pull_request", _full_pr_payload(), delivery_id="retry-me")

    # Isolation is preserved: inline TPM still completed before the handoff
    # error was re-raised to the HTTP boundary.
    mock_pub.assert_called_once()


def test_pull_request_threads_delivery_id_to_enqueue():
    """The X-GitHub-Delivery id must reach the enqueue so the async
    worker can key its idempotency claim on it (#272)."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": True}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation",
               return_value={"persona": "tpm", "result": "pass"}), \
         patch("async_dispatch.enqueue_elder_review", return_value=True) as mock_enq:
        mock_eval.return_value = type("R", (), {"passed": True})()
        dispatch("pull_request", _full_pr_payload(), delivery_id="deliv-abc")

    _, kwargs = mock_enq.call_args
    assert kwargs["delivery_id"] == "deliv-abc"
    assert kwargs["blocking"] is True  # plumbed from code_reviewer_blocking


def test_pull_request_missing_repo_id_runs_elder_and_tpm():
    """With Elder's missing_repo_policy now "enabled", a payload missing `repo_id`
    (a shape glitch) no longer skips Elder: both Chief (TPM) and Elder dispatch
    via the enabled default (`_handle_pull_request` line ~304). Elder enqueues
    with blocking=blocking_default (True) since there's no repo config to read.
    It degrades to neutral downstream if it cannot fetch the diff (fail-open)."""
    payload = _full_pr_payload()
    payload["repository"].pop("id", None)

    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation",
               return_value={"persona": "tpm", "result": "pass"}), \
         patch("async_dispatch.enqueue_elder_review") as mock_enq:
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", payload)

    personas_ran = [p["persona"] for p in out["personas"]]
    assert "tpm" in personas_ran
    assert "code_reviewer" in personas_ran
    mock_enq.assert_called_once()
    # Assert the blocking contract, not just the enqueue: with no repo config to
    # read, Elder must dispatch at blocking_default (True), not silently non-blocking.
    assert mock_enq.call_args.kwargs.get("blocking") is True


def test_pull_request_code_reviewer_disabled_skips_only_elder():
    """When `code_reviewer_enabled=False`, the Elder dispatch is skipped
    but TPM still runs (and vice versa)."""
    def _only_tpm_enabled(install_id, repo_id, persona):
        return persona == "tpm"

    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", side_effect=_only_tpm_enabled), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation",
               return_value={"persona": "tpm", "result": "pass"}), \
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
            "account": {"login": "quadseven", "type": "User", "id": 100},
        },
        "sender": {"id": 100, "login": "quadseven"},
    }
    with patch("dispatcher.record_installation") as mock_rec, \
         patch("dispatcher.is_install_allowlisted", return_value=False):
        out = dispatch("installation", payload)
    assert out["status"] == "recorded" and out["action"] == "created"
    mock_rec.assert_called_once_with(
        install_id=555, account_login="quadseven", account_type="User",
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
    ruleset_name: str = "Grug - TPM Enforcement",
    ruleset_id: int = 42,
    install_id: int = 999,
    repo_id: int = 7777,
    repo_full_name: str = "quadseven/infra",
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


def test_pull_request_publish_success_runs_ticket_compliance():
    """Positive twin of the skip test above: the #550 early return made
    the #529 advisory invocation CONDITIONAL, and the advisory block
    swallows its own errors - an inverted sentinel comparison would
    silently kill compliance comments forever with no error anywhere.
    Pin that a clean publish still invokes run_ticket_compliance."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", side_effect=lambda *a: _only_tpm(a[2])), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation",
               return_value={"persona": "tpm", "result": "pass"}), \
         patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")), \
         patch("personas.tpm.ticket_compliance_run.run_ticket_compliance",
               return_value={"status": "ok"}) as mock_compliance:
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    assert out["personas"][0] == {"persona": "tpm", "result": "pass"}
    mock_compliance.assert_called_once()


# --- reply-mined learnings inbound handler (#670, ADR-0020) -----------------

def _review_reply_payload(**over):
    p = {
        "action": "created",
        "comment": {
            "id": 5001,
            "in_reply_to_id": 4000,
            "body": "we always prefer early returns here, monitoring tracks the codes",
            "user": {"login": "dev", "type": "User"},
        },
        "pull_request": {"number": 42, "user": {"login": "dev"}},
        "repository": {"id": 7777, "name": "infra", "owner": {"login": "quadseven"},
                       "full_name": "quadseven/infra"},
        "installation": {"id": 999},
        "sender": {"login": "dev"},
    }
    for k, v in over.items():
        if k in ("comment", "pull_request", "sender") and isinstance(v, dict):
            p[k] = {**p[k], **v}
        else:
            p[k] = v
    return p


def test_review_reply_enqueues_for_write_author():
    # The PR author teaches, AND has write perm -> enqueued, author threaded.
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("github_app_auth.with_install_token_retry", side_effect=lambda iid, fn: "admin"), \
         patch("rerun.enqueue_learn") as mock_enq:
        out = dispatch("pull_request_review_comment", _review_reply_payload())
    assert out == {"status": "enqueued", "kind": "learn"}
    kw = mock_enq.call_args.kwargs
    assert kw["repo"] == "quadseven/infra" and kw["parent_comment_id"] == 4000
    assert kw["comment_id"] == 5001 and kw["pr_number"] == 42
    assert kw["author"] == "dev"  # the reply sender is the teacher


def test_review_reply_fork_author_without_write_is_blocked():
    # THE poisoning guard: a fork contributor IS the PR author on their own
    # fork PR but has only read access -> must NOT be able to teach.
    payload = _review_reply_payload(
        pull_request={"number": 42, "user": {"login": "forkuser"}},
        sender={"login": "forkuser"},
    )
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("github_app_auth.with_install_token_retry", side_effect=lambda iid, fn: "read"), \
         patch("rerun.enqueue_learn") as mock_enq:
        out = dispatch("pull_request_review_comment", payload)
    assert out["status"] == "no_op" and "lacks write perm" in out["reason"]
    mock_enq.assert_not_called()


def test_review_reply_not_a_reply_is_no_op():
    payload = _review_reply_payload(comment={"in_reply_to_id": None})
    with patch("rerun.enqueue_learn") as mock_enq:
        out = dispatch("pull_request_review_comment", payload)
    assert out["status"] == "no_op" and out["reason"] == "not a reply"
    mock_enq.assert_not_called()


def test_review_reply_from_bot_is_ignored():
    payload = _review_reply_payload(comment={"user": {"login": "grug[bot]", "type": "Bot"}})
    with patch("rerun.enqueue_learn") as mock_enq:
        out = dispatch("pull_request_review_comment", payload)
    assert out["status"] == "no_op" and out["reason"] == "reply author is a bot"
    mock_enq.assert_not_called()


def test_review_reply_from_non_collaborator_is_blocked():
    # sender != PR author, and the permission lookup returns "read".
    payload = _review_reply_payload(sender={"login": "randopublic"})
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("github_app_auth.with_install_token_retry", side_effect=lambda iid, fn: "read"), \
         patch("rerun.enqueue_learn") as mock_enq:
        out = dispatch("pull_request_review_comment", payload)
    assert out["status"] == "no_op" and "lacks write perm" in out["reason"]
    mock_enq.assert_not_called()


def test_review_reply_reviewer_disabled_is_no_op():
    # Gated on the REVIEWER persona (learnings feed Elder), not tpm.
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=False), \
         patch("rerun.enqueue_learn") as mock_enq:
        out = dispatch("pull_request_review_comment", _review_reply_payload())
    assert out["status"] == "no_op" and "code_reviewer disabled" in out["reason"]
    mock_enq.assert_not_called()


def test_review_reply_write_collaborator_enqueues():
    payload = _review_reply_payload(sender={"login": "teammate"})
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("github_app_auth.with_install_token_retry", side_effect=lambda iid, fn: "write"), \
         patch("rerun.enqueue_learn") as mock_enq:
        out = dispatch("pull_request_review_comment", payload)
    assert out == {"status": "enqueued", "kind": "learn"}
    mock_enq.assert_called_once()


def test_review_reply_enqueue_sqs_failure_is_skip_not_500():
    # A botocore/SQS send failure must return skip, never bubble to a 500.
    class _Boom(Exception):
        pass
    def _raise_enqueue(**kw):
        raise _Boom("sqs unavailable")
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("github_app_auth.with_install_token_retry", side_effect=lambda iid, fn: "write"), \
         patch("rerun.enqueue_learn", side_effect=_raise_enqueue):
        out = dispatch("pull_request_review_comment", _review_reply_payload())
    assert out["status"] == "skip" and out["reason"] == "enqueue_failed"
