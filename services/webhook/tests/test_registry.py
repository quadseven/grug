"""Registry scaffold tests (#465) — LOCK the declarative persona table against
the current hand-wired behavior, so wiring the dispatcher to iterate the
registry (next step) is a verifiable no-behavior-change refactor.
"""

from __future__ import annotations

from personas import registry


def test_registered_personas():
    keys = {p.key for p in registry.REGISTRY}
    assert keys == {
        "tpm", "code_reviewer", "guard", "warder", "pulse", "smasher",
        "walkthrough",
    }


def test_smasher_is_advisory_async_optin():
    # Smasher (#469) runs author code in a Job (async) and is opt-in per repo
    # (default OFF); mutation findings are advisory so it has no blocking mode.
    smasher = registry.by_key("smasher")
    assert smasher.canonical == "smasher"
    assert smasher.dispatch_style == "async"
    assert smasher.enabled_default is False
    assert smasher.blocking_flag is None
    assert smasher.missing_repo_policy == "disabled"
    assert smasher.check_run_name == "Grug - Smasher"


def test_canonical_names_match_adr_0002():
    assert registry.by_key("tpm").canonical == "chief"
    assert registry.by_key("code_reviewer").canonical == "elder"
    assert registry.by_canonical("elder").key == "code_reviewer"
    assert registry.by_key("guard").canonical == "guard"


def test_check_run_names_match_the_persona_modules():
    # Each persona's check-run name is the authoritative constant in its
    # dispatch module.
    from personas.code_reviewer.dispatch import _CHECK_NAME as elder_check
    from personas.guard.dispatch import _CHECK_NAME as guard_check
    assert registry.by_key("code_reviewer").check_run_name == elder_check
    assert registry.by_key("guard").check_run_name == guard_check


def test_default_config_matches_store_ssot():
    # The registry-derived defaults MUST equal the store's current
    # _DEFAULT_PERSONA_CONFIG, so making the store generic later is drift-free.
    from adapters.pg_install_store import _DEFAULT_PERSONA_CONFIG
    assert registry.default_persona_config() == _DEFAULT_PERSONA_CONFIG


def test_missing_repo_policy_is_explicit():
    # Chief + Elder review every repo the app sees (enabled); Guard stays opt-in.
    # The registry records the choice instead of leaving it as dispatcher folklore.
    assert registry.by_key("tpm").missing_repo_policy == "enabled"
    assert registry.by_key("code_reviewer").missing_repo_policy == "enabled"
    assert registry.by_key("guard").missing_repo_policy == "disabled"


def test_dispatch_styles():
    assert registry.by_key("tpm").dispatch_style == "inline"
    assert registry.by_key("code_reviewer").dispatch_style == "async"
    assert registry.by_key("guard").dispatch_style == "async"


def test_blocking_flags():
    assert registry.by_key("tpm").blocking_flag is None
    assert registry.by_key("code_reviewer").blocking_flag == "code_reviewer_blocking"
    assert registry.by_key("guard").blocking_flag == "guard_blocking"


def test_live_personas_declare_pull_request_event():
    # Pin the two live personas' events per-key (NOT a for-all-specs
    # assertion - a future issue_comment-only persona like Pulse #472
    # must be registrable without failing this lock). The events field
    # is what the dispatcher loop filters on (ADR-0010).
    assert registry.by_key("tpm").events == ("pull_request",)
    assert registry.by_key("code_reviewer").events == ("pull_request",)
    assert registry.by_key("guard").events == ("pull_request",)


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
        if "pull_request" in p.events:
            assert callable(getattr(mod, "dispatch_pull_request", None)), (
                f"{p.dispatch_module} must expose dispatch_pull_request(ctx)"
            )
        # Scheduled personas (Pulse) still must IMPORT - a typo'd module
        # path fails here, not on the first cron tick.
