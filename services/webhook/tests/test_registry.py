"""Registry scaffold tests (#465) — LOCK the declarative persona table against
the current hand-wired behavior, so wiring the dispatcher to iterate the
registry (next step) is a verifiable no-behavior-change refactor.
"""

from __future__ import annotations

from personas import registry


def test_two_personas_registered():
    keys = {p.key for p in registry.REGISTRY}
    assert keys == {"tpm", "code_reviewer"}


def test_canonical_names_match_adr_0002():
    assert registry.by_key("tpm").canonical == "chief"
    assert registry.by_key("code_reviewer").canonical == "elder"
    assert registry.by_canonical("elder").key == "code_reviewer"


def test_check_run_names_match_the_persona_modules():
    # Elder's check-run name is the authoritative constant in dispatch.py.
    from personas.code_reviewer.dispatch import _CHECK_NAME as elder_check
    assert registry.by_key("code_reviewer").check_run_name == elder_check


def test_default_config_matches_store_ssot():
    # The registry-derived defaults MUST equal the store's current
    # _DEFAULT_PERSONA_CONFIG, so making the store generic later is drift-free.
    from adapters.pg_install_store import _DEFAULT_PERSONA_CONFIG
    assert registry.default_persona_config() == _DEFAULT_PERSONA_CONFIG


def test_missing_repo_policy_is_explicit_and_opposite():
    # The two live personas deliberately differ (Chief on, Elder off) - the
    # registry records the choice instead of leaving it as dispatcher folklore.
    assert registry.by_key("tpm").missing_repo_policy == "enabled"
    assert registry.by_key("code_reviewer").missing_repo_policy == "disabled"


def test_dispatch_styles():
    assert registry.by_key("tpm").dispatch_style == "inline"
    assert registry.by_key("code_reviewer").dispatch_style == "async"


def test_only_elder_has_a_blocking_flag():
    assert registry.by_key("tpm").blocking_flag is None
    assert registry.by_key("code_reviewer").blocking_flag == "code_reviewer_blocking"


def test_live_personas_declare_pull_request_event():
    # Pin the two live personas' events per-key (NOT a for-all-specs
    # assertion - a future issue_comment-only persona like Pulse #472
    # must be registrable without failing this lock). The events field
    # is what the dispatcher loop filters on (ADR-0010).
    assert registry.by_key("tpm").events == ("pull_request",)
    assert registry.by_key("code_reviewer").events == ("pull_request",)


def test_spec_rejects_nonconvention_enabled_flag():
    """Audit #477 H1: the store derives the enablement key as
    f"{persona}_enabled" (AST-attested shape), so a non-convention
    enabled_flag would silently fail OPEN to enabled forever. The spec
    must refuse to construct."""
    import dataclasses

    import pytest

    chief = registry.by_key("tpm")
    with pytest.raises(ValueError, match="enabled_flag"):
        dataclasses.replace(chief, enabled_flag="tpm_review_enabled")


def test_spec_rejects_blocking_default_without_flag():
    """Audit #477 M1: blocking_default=True with no blocking_flag would
    mean always-blocking with no repo-level off switch."""
    import dataclasses

    import pytest

    chief = registry.by_key("tpm")
    with pytest.raises(ValueError, match="blocking_flag"):
        dataclasses.replace(chief, blocking_flag=None, blocking_default=True)


def test_every_dispatch_module_imports_and_exposes_entrypoint():
    """dispatch_module is a string resolved at dispatch time - a typo
    would otherwise surface only on the first live delivery. Import each
    registered module and check the `dispatch_pull_request(ctx)`
    convention here instead (ADR-0010 'accepted negative')."""
    import importlib

    for p in registry.REGISTRY:
        mod = importlib.import_module(p.dispatch_module)
        assert callable(getattr(mod, "dispatch_pull_request", None)), (
            f"{p.dispatch_module} must expose dispatch_pull_request(ctx)"
        )
