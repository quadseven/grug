"""Tribe nomenclature SSOT + dual-accept cutover helpers."""

from __future__ import annotations

from personas import tribe
from personas.registry import by_canonical, by_key


def test_check_names_are_caveman():
    assert tribe.CHECK_CHIEF == "Grug - Chief"
    assert tribe.CHECK_ELDER == "Grug - Elder"
    assert tribe.CHECK_GUARD == "Grug - Guard"
    assert by_key("tpm").check_run_name == tribe.CHECK_CHIEF
    assert by_key("code_reviewer").check_run_name == tribe.CHECK_ELDER
    assert by_canonical("elder").check_run_name == tribe.CHECK_ELDER


def test_legacy_aliases_accepted_for_detection():
    em = "\u2014"
    assert tribe.is_same_check("Grug - Definition of Ready", tribe.CHECK_CHIEF)
    assert tribe.is_same_check("Grug - Code Review", tribe.CHECK_ELDER)
    assert tribe.is_same_check(f"Grug {em} Definition of Ready", tribe.CHECK_CHIEF)
    assert tribe.is_same_check(f"Grug {em} Code Review", tribe.CHECK_ELDER)
    assert tribe.is_same_check(f"Grug {em} Chief", tribe.CHECK_CHIEF)
    assert tribe.is_same_check("Grug - Chief", tribe.CHECK_CHIEF)
    assert not tribe.is_same_check("Grug - Guard", tribe.CHECK_CHIEF)
    assert tribe.primary_check_name(f"Grug {em} Elder") == tribe.CHECK_ELDER


def test_primary_maps_legacy_back():
    assert tribe.primary_check_name("Grug - Definition of Ready") == tribe.CHECK_CHIEF
    assert tribe.primary_check_name("Grug - Code Review") == tribe.CHECK_ELDER
    assert tribe.primary_check_name("Grug - Elder") == tribe.CHECK_ELDER


def test_registry_matches_modules():
    from personas.code_reviewer.dispatch import _CHECK_NAME as elder
    from personas.tpm.persona import _CHECK_NAME as chief
    from personas.guard.dispatch import _CHECK_NAME as guard
    assert elder == tribe.CHECK_ELDER
    assert chief == tribe.CHECK_CHIEF
    assert guard == tribe.CHECK_GUARD


def test_renamed_checks_keep_their_old_titles_as_aliases():
    """A live check-run, or a ruleset naming the old context, must keep
    resolving after the rename. Verified before renaming that no ruleset
    REQUIRED any of these three (only `Grug - Chief` is required across
    grug and two private repos) - but a stale reference must still resolve, not
    silently detach."""
    from personas import tribe

    assert tribe.CHECK_SENTINEL == "Grug - Haunt"
    assert tribe.CHECK_WARDER == "Grug - Totem"
    assert tribe.CHECK_PULSE == "Grug - Drum"

    for legacy, primary in (
        ("Grug - Sentinel", tribe.CHECK_SENTINEL),
        ("Grug - Warder", tribe.CHECK_WARDER),
        ("Grug - Pulse", tribe.CHECK_PULSE),
    ):
        assert tribe._ALIAS_TO_PRIMARY.get(legacy) == primary, legacy


def test_chief_title_is_untouched():
    """`Grug - Chief` is the ONE required status check in every ruleset.
    Renaming it would silently detach enforcement fleet-wide."""
    from personas import tribe
    assert tribe.CHECK_CHIEF == "Grug - Chief"
