"""Registry-driven dispatch tests (#465 step 2, ADR-0010).

The acceptance criterion these pin: adding a persona = ONE PersonaSpec
entry + ONE module exposing `dispatch_pull_request(ctx)` - no dispatcher
edits, no store edits. A toy persona is injected purely through the
registry seam and must dispatch alongside the real two, isolated from
their failures and vice versa.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import personas.tpm.persona  # noqa: F401 - register submodule for patch path
from personas import registry as persona_registry
from dispatcher import dispatch


def _full_pr_payload():
    return {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "body": "## Why\nbecause\n## Acceptance criteria\n- a\n- b\n- c\n## Out of scope\nx\nSize: S\ncloses #1",
            "head": {"sha": "abc123def456"},
        },
        "repository": {"id": 7777, "name": "infra", "owner": {"login": "githumps"}, "full_name": "githumps/infra"},
        "installation": {"id": 999},
    }


def _toy_module(record: list) -> types.ModuleType:
    mod = types.ModuleType("toy_webhook_dispatch")

    def dispatch_pull_request(ctx):
        record.append(ctx)
        return {"persona": "toy", "result": "pass"}

    mod.dispatch_pull_request = dispatch_pull_request
    return mod


def _toy_spec() -> persona_registry.PersonaSpec:
    return persona_registry.PersonaSpec(
        key="toy",
        canonical="toy",
        check_run_name="Grug - Toy",
        enabled_flag="toy_enabled",
        enabled_default=True,
        blocking_flag=None,
        blocking_default=False,
        dispatch_style="inline",
        missing_repo_policy="enabled",
        events=("pull_request",),
        dispatch_module="toy_webhook_dispatch",
    )


def test_toy_persona_dispatches_via_registry_only(monkeypatch):
    """One registry entry + one module = a dispatched persona. No edits
    to dispatcher.py or the store are involved anywhere in this test."""
    seen: list = []
    monkeypatch.setitem(sys.modules, "toy_webhook_dispatch", _toy_module(seen))
    extended = persona_registry.REGISTRY + (_toy_spec(),)

    with patch.object(persona_registry, "REGISTRY", extended), \
         patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation"), \
         patch("async_dispatch.enqueue_elder_review", return_value=True):
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload(), delivery_id="deliv-toy")

    assert out["status"] == "dispatched"
    assert [r["persona"] for r in out["personas"]] == ["tpm", "code_reviewer", "toy"]
    assert out["personas"][2] == {"persona": "toy", "result": "pass"}

    # The uniform context contract: the toy module received the full
    # event coordinates without any toy-specific dispatcher plumbing.
    ctx = seen[0]
    assert ctx.installation_id == 999
    assert ctx.owner == "githumps"
    assert ctx.repo_name == "infra"
    assert ctx.head_sha == "abc123def456"
    assert ctx.pr_number == 42
    assert ctx.delivery_id == "deliv-toy"
    assert ctx.blocking is False
    assert ctx.payload["action"] == "opened"


def test_toy_persona_exception_is_isolated(monkeypatch):
    """A broken persona module (raises at dispatch) must record
    unhandled_error for ITSELF and leave the other personas' results
    intact - the loop-level guard, not the module, provides isolation."""
    mod = types.ModuleType("toy_webhook_dispatch")

    def dispatch_pull_request(ctx):
        raise RuntimeError("toy exploded")

    mod.dispatch_pull_request = dispatch_pull_request
    monkeypatch.setitem(sys.modules, "toy_webhook_dispatch", mod)
    extended = persona_registry.REGISTRY + (_toy_spec(),)

    with patch.object(persona_registry, "REGISTRY", extended), \
         patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation"), \
         patch("async_dispatch.enqueue_elder_review", return_value=True):
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    assert out["personas"][0]["result"] == "pass"
    assert out["personas"][1] == {"persona": "code_reviewer", "result": "queued"}
    assert out["personas"][2] == {"persona": "toy", "result": "unhandled_error"}


def test_toy_persona_missing_module_is_isolated(monkeypatch):
    """dispatch_module is a string resolved at dispatch time - a typo'd
    or missing module must degrade to unhandled_error for that persona
    only (the import failure happens inside the per-persona guard)."""
    spec = persona_registry.PersonaSpec(
        key="toy",
        canonical="toy",
        check_run_name="Grug - Toy",
        enabled_flag="toy_enabled",
        enabled_default=True,
        blocking_flag=None,
        blocking_default=False,
        dispatch_style="inline",
        missing_repo_policy="enabled",
        events=("pull_request",),
        dispatch_module="module_that_does_not_exist_465",
    )
    extended = persona_registry.REGISTRY + (spec,)

    with patch.object(persona_registry, "REGISTRY", extended), \
         patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("dispatcher.get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation"), \
         patch("async_dispatch.enqueue_elder_review", return_value=True):
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _full_pr_payload())

    assert out["personas"][2] == {"persona": "toy", "result": "unhandled_error"}
    assert out["personas"][0]["result"] == "pass"


def test_toy_persona_missing_repo_policy_disabled_skips(monkeypatch):
    """missing_repo_policy is registry data, not dispatcher folklore: a
    `disabled` toy persona must be skipped when the payload lacks
    repository.id, without is_persona_enabled ever being called for it."""
    seen: list = []
    monkeypatch.setitem(sys.modules, "toy_webhook_dispatch", _toy_module(seen))
    spec = persona_registry.PersonaSpec(
        key="toy",
        canonical="toy",
        check_run_name="Grug - Toy",
        enabled_flag="toy_enabled",
        enabled_default=True,
        blocking_flag=None,
        blocking_default=False,
        dispatch_style="inline",
        missing_repo_policy="disabled",
        events=("pull_request",),
        dispatch_module="toy_webhook_dispatch",
    )
    payload = _full_pr_payload()
    payload["repository"].pop("id", None)
    extended = persona_registry.REGISTRY + (spec,)

    with patch.object(persona_registry, "REGISTRY", extended), \
         patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled", return_value=True), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation"), \
         patch("async_dispatch.enqueue_elder_review") as mock_enq:
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", payload)

    # TPM (policy: enabled) ran; Elder + toy (policy: disabled) skipped.
    assert [r["persona"] for r in out["personas"]] == ["tpm"]
    assert seen == []
    mock_enq.assert_not_called()


def test_pull_request_review_falls_through_to_generic_no_op():
    """The v1.5 placeholder branch is retired (#465): the event now hits
    the generic no-handler fallthrough instead of a bespoke reason."""
    out = dispatch("pull_request_review", {})
    assert out["status"] == "no_op"
    assert "no handler" in out["reason"]
