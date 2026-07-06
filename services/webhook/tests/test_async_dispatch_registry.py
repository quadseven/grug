"""Registry <-> async-machinery coverage guard (#77, ADR-0014).

The generic enqueue+run in async_dispatch.py is driven by the
_ASYNC_PERSONAS spec table. A NEW async persona added to the registry
(dispatch_style="async") without a matching table row would enqueue
nothing — this suite makes that drift a red test instead of a silent
production no-op, and pins the persona-specific contract values the
generalization must never normalize away.
"""

from __future__ import annotations

import async_dispatch as ad
from personas.registry import REGISTRY


def _registry_async_keys() -> set[str]:
    return {spec.key for spec in REGISTRY if spec.dispatch_style == "async"}


def test_every_async_registry_persona_has_machinery():
    assert _registry_async_keys() == set(ad._ASYNC_PERSONAS.keys())


def test_runner_names_resolve_to_module_globals():
    # _spawn_local resolves runners late through globals() so tests can
    # patch them; a typo'd runner_name would only explode inside a daemon
    # thread at enqueue time. Resolve them all here instead.
    for key, spec in ad._ASYNC_PERSONAS.items():
        runner = getattr(ad, spec.runner_name, None)
        assert callable(runner), f"{key}: runner {spec.runner_name} not found"


def test_dispatch_paths_resolve_to_callables():
    # Same drift class one layer deeper: a typo'd dispatch_path would only
    # surface inside _run_job's final try as *_job_unhandled + a
    # self-recover enqueue per job. Resolve every path statically instead
    # (audit stage 1 finding).
    for key, spec in ad._ASYNC_PERSONAS.items():
        dispatch = ad._resolve_dispatch(spec.dispatch_path)
        assert callable(dispatch), f"{key}: dispatch {spec.dispatch_path} not callable"


def test_job_kinds_and_log_prefixes_are_unique_and_pinned():
    kinds = [s.job_kind for s in ad._ASYNC_PERSONAS.values()]
    prefixes = [s.log_prefix for s in ad._ASYNC_PERSONAS.values()]
    assert len(set(kinds)) == len(kinds)
    # Monitored DD log-line names derive from these prefixes
    # (f"{prefix}_job_done" etc.) — changing one silently blinds a monitor.
    assert set(prefixes) == {"elder", "guard", "smasher", "walkthrough"}


def test_elder_legacy_claim_and_persona_values_are_pinned():
    """Elder's quirks are LOAD-BEARING legacy, not table typos:
    raw-GUID delivery claim (#272 rows exist in the store), claim_review
    persona `code_reviewer` (historical key), rerun persona `elder`.
    Guard/Smasher claim namespaced because all three personas dispatch
    from the SAME delivery GUID. Pinned at the BEHAVIOR level
    (claim_key output), not just the field, so a derivation change
    cannot slip past a field-only assertion."""
    elder = ad._ASYNC_PERSONAS["code_reviewer"]
    assert elder.claim_key("guid-123") == "guid-123"
    assert elder.review_persona == "code_reviewer"
    assert elder.rerun_persona == "elder"
    for key in ("guard", "smasher"):
        spec = ad._ASYNC_PERSONAS[key]
        assert spec.claim_key("guid-123") == f"guid-123:{key}"
        assert spec.review_persona == key
        assert spec.rerun_persona == key


def test_thread_names_stay_length_bounded():
    for spec in ad._ASYNC_PERSONAS.values():
        guid_chars = ad._THREAD_NAME_LEN - len(spec.log_prefix) - 1
        name = f"{spec.log_prefix}-{'d' * 64:.{guid_chars}s}"
        assert len(name) == ad._THREAD_NAME_LEN


def test_post_init_rejects_illegal_contract_values():
    """The __post_init__ guards exist to catch future table typos at
    import time - prove each branch actually fires (audit stage 7)."""
    import dataclasses

    import pytest

    valid = ad._ASYNC_PERSONAS["guard"]
    for illegal in (
        {"claim_namespace": ""},
        {"dispatch_path": "no.colon.here"},
        {"runner_name": "handle_guard"},
        {"log_prefix": ""},
        {"log_prefix": "x" * ad._THREAD_NAME_LEN},
    ):
        with pytest.raises(ValueError):
            dataclasses.replace(valid, **illegal)


def test_image_smoke_covers_every_lazy_dispatch_module():
    """check.image-build.yml's webhook mods list is hand-maintained; a
    future async persona missing from it would pass the gate and fail
    inside every async job in-image - the exact hole the gate closes
    (audit stage 7)."""
    from pathlib import Path

    wf = (
        Path(__file__).resolve().parents[3]
        / ".github/workflows/check.image-build.yml"
    ).read_text()
    for spec in ad._ASYNC_PERSONAS.values():
        module = spec.dispatch_path.split(":", 1)[0]
        assert module in wf, (
            f"{module} missing from the check.image-build.yml import smoke"
        )
