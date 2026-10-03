"""Tests for webhook → persona dispatcher.

Covers routing decisions, payload-shape gates, allowlist gating, and
installation event handling. TPM evaluator + install_store are
patched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import personas.tpm.persona  # noqa: F401 — register submodule for patch path
import pytest
from dispatcher import dispatch
from personas import registry as persona_registry


def test_unknown_event_no_op():
    out = dispatch("issues", {})
    assert out["status"] == "no_op"


def test_pull_request_review_placeholder():
    out = dispatch("pull_request_review", {})
    assert out["status"] == "no_op" and "code-reviewer" not in out["reason"]


def test_installation_repositories_no_id_skips():
    out = dispatch("installation_repositories", {})
    assert out["status"] == "skip"


def test_installation_repositories_removed_no_ops():
    """grug#833 out of scope: a removed repo's ruleset goes with it."""
    payload = {
        "action": "removed",
        "installation": {"id": 555},
        "repositories_removed": [{"id": 1, "full_name": "quadseven/gone"}],
    }
    with patch("dispatcher._enforce_on_repos") as mock_enforce:
        out = dispatch("installation_repositories", payload)
    assert out["status"] == "no_op"
    mock_enforce.assert_not_called()


def test_installation_repositories_added_enforces_exactly_those_repos():
    """grug#833: a repo added to an EXISTING install used to sit ungated -
    dispatcher no-op'd this event entirely and only a persona toggle ever
    called `_enforce_on_repos`. Live 2026-08-08: a private fleet repo was
    ungated for >1h until the enforcement-gap monitor fired."""
    payload = {
        "action": "added",
        "installation": {"id": 555},
        "repositories_added": [
            {"id": 1, "full_name": "quadseven/new-repo", "default_branch": "main"},
        ],
        "repositories_removed": [],
    }
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher._enforce_on_repos") as mock_enforce:
        out = dispatch("installation_repositories", payload)
    assert out["status"] == "recorded" and out["action"] == "repositories_added"
    mock_enforce.assert_called_once_with(555, payload["repositories_added"])


def test_installation_repositories_added_skips_non_allowlisted():
    """Same defense-in-depth allowlist gate as the `installation` created
    path (Slice 5 #26) - a non-allowlisted install's added repos are never
    auto-enforced."""
    payload = {
        "action": "added",
        "installation": {"id": 555},
        "repositories_added": [{"id": 1, "full_name": "quadseven/new-repo"}],
    }
    with patch("dispatcher.is_install_allowlisted", return_value=False), \
         patch("dispatcher._enforce_on_repos") as mock_enforce:
        out = dispatch("installation_repositories", payload)
    assert out["status"] == "recorded"
    mock_enforce.assert_not_called()


def test_pull_request_skip_personas_omits_named_persona_only(monkeypatch):
    """grug#947: the reconciler passes skip_personas for a persona whose
    check-run already exists on this head SHA - dispatch must skip
    exactly that one and still run everything else enabled, never both
    or neither."""
    import personas.code_reviewer.webhook_dispatch

    mock_tpm_eval = MagicMock(return_value=type("R", (), {"passed": True})())
    mock_tpm_pub = MagicMock(return_value={"persona": "tpm", "result": "pass"})
    mock_cr_dispatch = MagicMock(return_value={"persona": "code_reviewer", "result": "queued"})
    monkeypatch.setattr(
        personas.code_reviewer.webhook_dispatch, "dispatch_pull_request", mock_cr_dispatch,
    )
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch(
             "dispatcher.is_persona_enabled",
             side_effect=lambda *a: a[2] in {"tpm", "code_reviewer"},
         ), \
         patch("dispatcher.get_repo_config", return_value={}), \
         patch("personas.tpm.persona.evaluate_pull_request", mock_tpm_eval), \
         patch("personas.tpm.persona.publish_tpm_evaluation", mock_tpm_pub):
        out = dispatch(
            "pull_request", _full_pr_payload(), skip_personas=frozenset({"tpm"}),
        )

    assert out["status"] == "dispatched"
    keys = {p["persona"] for p in out["personas"]}
    assert keys == {"code_reviewer"}, "tpm must be skipped, code_reviewer must still run"
    mock_tpm_eval.assert_not_called()
    mock_cr_dispatch.assert_called_once()


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
        "sender": {"id": 100, "login": "alice"},
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
        "installation": {"id": 555, "account": {"login": "alice", "type": "User", "id": 100}},
        "sender": {"id": 100, "login": "alice"},
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


# --- issues event: Chief's issue-time DoR surface ---------------------------
# Grug's FIRST non-PR review surface. These pin the gates, not the rendering
# (that lives in test_issue_dor.py) - a write-enabled surface reached by a
# public webhook has to fail CLOSED on every ambiguity.

def _issue_payload(**over):
    p = {
        "action": "opened",
        "issue": {"number": 7, "body": "bare"},
        "repository": {"id": 5, "name": "r", "owner": {"login": "o"}},
        "installation": {"id": 1},
    }
    p.update(over)
    return p


@pytest.mark.parametrize("action", ["closed", "labeled", "assigned", "deleted"])
def test_issues_only_gates_opened_and_edited(action):
    out = dispatch("issues", _issue_payload(action=action))
    assert out["status"] == "no_op" and "not gated" in out["reason"]


def test_issues_event_carrying_a_pull_request_is_refused():
    """An `issues` event never fires for a PR today. If GitHub ever changed
    that, commenting here would DOUBLE up with the PR-time DoR check."""
    out = dispatch("issues", _issue_payload(
        issue={"number": 7, "body": "x", "pull_request": {"url": "..."}}))
    assert out["status"] == "no_op" and "on a PR" in out["reason"]


def test_issues_requires_allowlisted_install():
    with patch("dispatcher.is_install_allowlisted", return_value=False):
        out = dispatch("issues", _issue_payload())
    assert out["status"] == "no_op" and "not allowlisted" in out["reason"]


def test_issues_missing_repo_id_fails_closed():
    """Chief's PR path treats a missing repo_id as ENABLED (a missing id
    must not skip DoR). This surface WRITES a public comment, so it fails
    the other way - defaulting a write ON for a malformed payload is the
    wrong direction."""
    with patch("dispatcher.is_install_allowlisted", return_value=True):
        out = dispatch("issues", _issue_payload(
            repository={"name": "r", "owner": {"login": "o"}}))
    assert out["status"] == "no_op" and "fails closed" in out["reason"]


def test_issues_is_off_unless_its_own_flag_is_on():
    """Must NOT ride `tpm_enabled`: Chief being on for pull requests cannot
    silently start commenting on every issue in the repo."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"tpm_enabled": True}):
        out = dispatch("issues", _issue_payload())
    assert out["status"] == "no_op" and "issue_dor_enabled off" in out["reason"]


def test_issues_dispatches_when_flag_on():
    seen = {}

    def _run(token, owner, repo, number, body, *, fetch_facts, refresh_only):
        seen.update(owner=owner, repo=repo, number=number, body=body,
                    refresh_only=refresh_only, has_facts=callable(fetch_facts))
        return {"status": "ok", "reason": "advisory posted"}

    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"issue_dor_enabled": True}), \
         patch("personas.tpm.issue_dor.run_issue_dor", _run), \
         patch("github_app_auth.with_install_token_retry",
               side_effect=lambda iid, fn: fn("tok")):
        out = dispatch("issues", _issue_payload())
    assert out["status"] == "ok"
    assert seen == {"owner": "o", "repo": "r", "number": 7, "body": "bare",
                    "refresh_only": False, "has_facts": True}


def test_issues_incomplete_payload_skips():
    out = dispatch("issues", _issue_payload(installation={}))
    assert out["status"] == "skip"


# --- sub_issues: a link changed, refresh the advisory (grug#1035) ------------

def _sub_payload(action):
    return {
        "action": action,
        "sub_issue": {"number": 7, "body": "child body"},
        "parent_issue": {"number": 3, "body": "parent body"},
        "repository": {"id": 5, "name": "r", "owner": {"login": "o"}},
        "installation": {"id": 1},
    }


@pytest.mark.parametrize("action,number,body", [
    ("parent_issue_added", 7, "child body"),
    ("parent_issue_removed", 7, "child body"),
    ("sub_issue_added", 3, "parent body"),
    ("sub_issue_removed", 3, "parent body"),
])
def test_sub_issues_refreshes_the_issue_whose_membership_changed(action, number, body):
    """The child's `epic` line clears or returns; the parent becomes or stops
    being an epic. Always refresh-only: a link event never starts a comment."""
    seen = {}

    def _run(token, owner, repo, n, b, *, fetch_facts, refresh_only):
        seen.update(number=n, body=b, refresh_only=refresh_only)
        return {"status": "ok", "reason": "advisory refreshed"}

    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"issue_dor_enabled": True}), \
         patch("personas.tpm.issue_dor.run_issue_dor", _run), \
         patch("github_app_auth.with_install_token_retry",
               side_effect=lambda iid, fn: fn("tok")):
        out = dispatch("sub_issues", _sub_payload(action))
    assert out["status"] == "ok"
    assert seen == {"number": number, "body": body, "refresh_only": True}


def test_sub_issues_other_actions_are_not_gated():
    out = dispatch("sub_issues", _sub_payload("transferred"))
    assert out["status"] == "no_op" and "not gated" in out["reason"]


def test_sub_issues_respects_the_repo_flag():
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={}):
        out = dispatch("sub_issues", _sub_payload("parent_issue_added"))
    assert out["status"] == "no_op" and "issue_dor_enabled off" in out["reason"]


# --- check_run / check_suite rerequested (grug#948) -------------------------
# GitHub's own "Re-run" button. check_run carries a single named check
# (re-dispatch only the persona that owns it); check_suite carries no name
# (re-dispatch everyone enabled, same as a fresh pull_request webhook).

def _check_run_payload(**over):
    p = {
        "action": "rerequested",
        "check_run": {
            "name": "Grug - Chief",
            "head_sha": "abc123",
            "pull_requests": [
                {"number": 42, "id": 1, "head": {"sha": "abc123"}, "base": {"sha": "basesha"}},
            ],
        },
        "repository": {"id": 7777, "name": "infra", "owner": {"login": "quadseven"},
                       "full_name": "quadseven/infra"},
        "installation": {"id": 999},
    }
    for k, v in over.items():
        if k in ("check_run", "repository") and isinstance(v, dict):
            p[k] = {**p[k], **v}
        else:
            p[k] = v
    return p


def _check_suite_payload(**over):
    p = {
        "action": "rerequested",
        "check_suite": {
            "pull_requests": [
                {"number": 42, "id": 1, "head": {"sha": "abc123"}, "base": {"sha": "basesha"}},
            ],
        },
        "repository": {"id": 7777, "name": "infra", "owner": {"login": "quadseven"},
                       "full_name": "quadseven/infra"},
        "installation": {"id": 999},
    }
    for k, v in over.items():
        if k in ("check_suite", "repository") and isinstance(v, dict):
            p[k] = {**p[k], **v}
        else:
            p[k] = v
    return p


def test_check_run_unhandled_action_no_ops():
    out = dispatch("check_run", _check_run_payload(action="completed"))
    assert out["status"] == "no_op" and "completed" in out["reason"]


def test_check_run_unknown_name_no_ops():
    """A check-run belonging to some other GitHub App entirely - must not
    match any persona and must not dispatch anything."""
    out = dispatch("check_run", _check_run_payload(check_run={"name": "Some Other App"}))
    assert out["status"] == "no_op" and "not ours" in out["reason"]


def test_check_run_incomplete_payload_skips():
    out = dispatch(
        "check_run",
        _check_run_payload(installation={}),
    )
    assert out["status"] == "skip" and out["reason"] == "incomplete_payload_or_no_linked_pr"


def test_check_run_no_linked_pr_skips():
    out = dispatch("check_run", _check_run_payload(check_run={"pull_requests": []}))
    assert out["status"] == "skip" and out["reason"] == "incomplete_payload_or_no_linked_pr"


def test_check_run_not_allowlisted_no_ops():
    with patch("dispatcher.is_install_allowlisted", return_value=False), \
         patch("dispatcher._dispatch_rerequest_for_pr") as mock_redispatch:
        out = dispatch("check_run", _check_run_payload())
    assert out["status"] == "no_op" and "not allowlisted" in out["reason"]
    mock_redispatch.assert_not_called()


def test_check_run_rerequest_skips_every_other_persona():
    """The whole point of #948's check_run handling: a rerun of just
    "Grug - Chief" must not also re-trigger Elder, Guard, etc."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher._dispatch_rerequest_for_pr",
               return_value={"status": "ok"}) as mock_redispatch:
        out = dispatch("check_run", _check_run_payload())

    assert out == {
        "status": "dispatched", "trigger": "check_run_rerequested",
        "persona": "tpm", "results": [{"status": "ok"}],
    }
    mock_redispatch.assert_called_once()
    args = mock_redispatch.call_args.args
    assert args[0] == 999 and args[1] == "quadseven" and args[2] == "infra" and args[3] == 7777
    assert args[4] == {"number": 42, "id": 1, "head": {"sha": "abc123"}, "base": {"sha": "basesha"}}
    skip_personas = args[5]
    assert "tpm" not in skip_personas
    assert skip_personas == frozenset(
        spec.key for spec in persona_registry.REGISTRY if spec.key != "tpm"
    )


def test_check_run_dispatches_one_rerequest_per_linked_pr():
    payload = _check_run_payload(check_run={
        "pull_requests": [
            {"number": 42, "id": 1, "head": {"sha": "a"}, "base": {"sha": "b"}},
            {"number": 43, "id": 2, "head": {"sha": "c"}, "base": {"sha": "d"}},
        ],
    })
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher._dispatch_rerequest_for_pr",
               return_value={"status": "ok"}) as mock_redispatch:
        out = dispatch("check_run", payload)
    assert len(out["results"]) == 2
    assert mock_redispatch.call_count == 2


def test_check_suite_unhandled_action_no_ops():
    out = dispatch("check_suite", _check_suite_payload(action="completed"))
    assert out["status"] == "no_op" and "completed" in out["reason"]


def test_check_suite_incomplete_payload_skips():
    out = dispatch("check_suite", _check_suite_payload(check_suite={"pull_requests": []}))
    assert out["status"] == "skip" and out["reason"] == "incomplete_payload_or_no_linked_pr"


def test_check_suite_not_allowlisted_no_ops():
    with patch("dispatcher.is_install_allowlisted", return_value=False), \
         patch("dispatcher._dispatch_rerequest_for_pr") as mock_redispatch:
        out = dispatch("check_suite", _check_suite_payload())
    assert out["status"] == "no_op" and "not allowlisted" in out["reason"]
    mock_redispatch.assert_not_called()


def test_check_suite_rerequest_skips_nobody():
    """A "re-run all checks" click - every enabled persona should run,
    same as a fresh pull_request webhook, unlike the single-check case."""
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher._dispatch_rerequest_for_pr",
               return_value={"status": "ok"}) as mock_redispatch:
        out = dispatch("check_suite", _check_suite_payload())

    assert out == {
        "status": "dispatched", "trigger": "check_suite_rerequested",
        "results": [{"status": "ok"}],
    }
    mock_redispatch.assert_called_once()
    assert mock_redispatch.call_args.args[5] == frozenset()


def test_dispatch_rerequest_for_pr_no_number_skips():
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher._fetch_pr_for_rerequest") as mock_fetch:
        out = dispatch(
            "check_suite",
            _check_suite_payload(check_suite={"pull_requests": [{"head": {"sha": "x"}}]}),
        )
    assert out["results"][0] == {"status": "skip", "reason": "pull_requests entry has no number"}
    mock_fetch.assert_not_called()


def test_dispatch_rerequest_for_pr_fetch_failure_skips():
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher._fetch_pr_for_rerequest", return_value=None), \
         patch("dispatcher.dispatch") as mock_dispatch:
        out = dispatch("check_suite", _check_suite_payload())
    assert out["results"][0] == {"status": "skip", "reason": "pr_fetch_failed", "pr_number": 42}
    mock_dispatch.assert_not_called()


def test_dispatch_rerequest_for_pr_no_head_sha_skips():
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher._fetch_pr_for_rerequest", return_value={"number": 42, "head": {}}), \
         patch("dispatcher.dispatch") as mock_dispatch:
        out = dispatch("check_suite", _check_suite_payload())
    assert out["results"][0] == {"status": "skip", "reason": "pr_has_no_head_sha", "pr_number": 42}
    mock_dispatch.assert_not_called()


def test_dispatch_rerequest_for_pr_builds_synthetic_pull_request_event():
    """The re-fetched PR's real `body` must flow into the synthetic event -
    the raw check_run/check_suite payload only ever carries a minimal
    pull_requests entry with no body, and TPM's DoR check needs the real
    one."""
    pr_json = {"number": 42, "body": "## Why\nreal body\n", "head": {"sha": "freshsha"}}
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher._fetch_pr_for_rerequest", return_value=pr_json), \
         patch("dispatcher.dispatch") as mock_dispatch:
        mock_dispatch.return_value = {"status": "dispatched", "personas": []}
        dispatch("check_suite", _check_suite_payload())

    mock_dispatch.assert_called_once()
    event_name, synthetic = mock_dispatch.call_args.args
    assert event_name == "pull_request"
    assert mock_dispatch.call_args.kwargs["skip_personas"] == frozenset()
    assert synthetic["action"] == "synchronize"
    assert synthetic["pull_request"] == {
        "number": 42, "body": "## Why\nreal body\n", "head": {"sha": "freshsha"},
    }
    assert synthetic["repository"] == {
        "id": 7777, "name": "infra", "full_name": "quadseven/infra",
        "owner": {"login": "quadseven"},
    }
    assert synthetic["installation"] == {"id": 999}


def test_dispatch_rerequest_for_pr_missing_body_defaults_to_empty_string():
    pr_json = {"number": 42, "head": {"sha": "freshsha"}}  # no "body" key at all
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher._fetch_pr_for_rerequest", return_value=pr_json), \
         patch("dispatcher.dispatch") as mock_dispatch:
        mock_dispatch.return_value = {"status": "dispatched", "personas": []}
        dispatch("check_suite", _check_suite_payload())
    assert mock_dispatch.call_args.args[1]["pull_request"]["body"] == ""


def test_fetch_pr_for_rerequest_hits_correct_url_with_real_token_wiring():
    """End-to-end through the real `_fetch_pr_for_rerequest` + the real
    `with_install_token_retry` wiring (only `httpx.get` itself is mocked) -
    pins the URL shape and confirms the fetched body reaches the synthetic
    payload."""
    pr_resp = MagicMock(spec=httpx.Response)
    pr_resp.raise_for_status = MagicMock()
    pr_resp.json = MagicMock(return_value={
        "number": 42, "body": "fetched live", "head": {"sha": "livesha"},
    })
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("github_app_auth.with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get", return_value=pr_resp) as mock_get, \
         patch("dispatcher.dispatch") as mock_dispatch:
        mock_dispatch.return_value = {"status": "dispatched", "personas": []}
        dispatch("check_suite", _check_suite_payload())

    mock_get.assert_called_once()
    url = mock_get.call_args.args[0]
    assert url == "https://api.github.com/repos/quadseven/infra/pulls/42"
    headers = mock_get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "token tok"
    synthetic = mock_dispatch.call_args.args[1]
    assert synthetic["pull_request"]["body"] == "fetched live"
    assert synthetic["pull_request"]["head"]["sha"] == "livesha"


def test_fetch_pr_for_rerequest_http_status_error_skips_that_pr():
    """A permanent 4xx/5xx on the re-fetch must skip just this PR, not
    crash the whole rerequest (other linked PRs, if any, still proceed)."""
    resp = MagicMock(status_code=404)
    err = httpx.HTTPStatusError("not found", request=MagicMock(), response=resp)
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("github_app_auth.with_install_token_retry", side_effect=err), \
         patch("dispatcher.dispatch") as mock_dispatch:
        out = dispatch("check_suite", _check_suite_payload())
    assert out["results"][0] == {"status": "skip", "reason": "pr_fetch_failed", "pr_number": 42}
    mock_dispatch.assert_not_called()


def test_fetch_pr_for_rerequest_transport_error_skips_that_pr():
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("github_app_auth.with_install_token_retry",
               side_effect=httpx.RequestError("github unreachable")), \
         patch("dispatcher.dispatch") as mock_dispatch:
        out = dispatch("check_suite", _check_suite_payload())
    assert out["results"][0] == {"status": "skip", "reason": "pr_fetch_failed", "pr_number": 42}
    mock_dispatch.assert_not_called()
