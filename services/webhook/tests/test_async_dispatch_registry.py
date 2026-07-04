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


def test_job_kinds_and_log_prefixes_are_unique_and_pinned():
    kinds = [s.job_kind for s in ad._ASYNC_PERSONAS.values()]
    prefixes = [s.log_prefix for s in ad._ASYNC_PERSONAS.values()]
    assert len(set(kinds)) == len(kinds)
    # Monitored DD log-line names derive from these prefixes
    # (f"{prefix}_job_done" etc.) — changing one silently blinds a monitor.
    assert set(prefixes) == {"elder", "guard", "smasher"}


def test_elder_legacy_claim_and_persona_values_are_pinned():
    """Elder's quirks are LOAD-BEARING legacy, not table typos:
    raw-GUID delivery claim (#272 rows exist in the store), claim_review
    persona `code_reviewer` (historical key), rerun persona `elder`.
    Guard/Smasher claim namespaced because all three personas dispatch
    from the SAME delivery GUID."""
    elder = ad._ASYNC_PERSONAS["code_reviewer"]
    assert elder.claim_namespace is None
    assert elder.review_persona == "code_reviewer"
    assert elder.rerun_persona == "elder"
    for key in ("guard", "smasher"):
        spec = ad._ASYNC_PERSONAS[key]
        assert spec.claim_namespace == key
        assert spec.review_persona == key
        assert spec.rerun_persona == key


def test_thread_names_stay_length_bounded():
    for spec in ad._ASYNC_PERSONAS.values():
        guid_chars = ad._THREAD_NAME_LEN - len(spec.log_prefix) - 1
        name = f"{spec.log_prefix}-{'d' * 64:.{guid_chars}s}"
        assert len(name) == ad._THREAD_NAME_LEN
