"""Tribe nomenclature SSOT + dual-accept cutover helpers."""

from __future__ import annotations

from personas import tribe
from personas.registry import by_canonical, by_key


def test_check_names_are_caveman():
    assert tribe.CHECK_CHIEF == "Grug — Chief"
    assert tribe.CHECK_ELDER == "Grug — Elder"
    assert tribe.CHECK_GUARD == "Grug — Guard"
    assert by_key("tpm").check_run_name == tribe.CHECK_CHIEF
    assert by_key("code_reviewer").check_run_name == tribe.CHECK_ELDER
    assert by_canonical("elder").check_run_name == tribe.CHECK_ELDER


def test_legacy_aliases_accepted_for_detection():
    assert tribe.is_same_check("Grug — Definition of Ready", tribe.CHECK_CHIEF)
    assert tribe.is_same_check("Grug — Code Review", tribe.CHECK_ELDER)
    assert tribe.is_same_check("Grug — Chief", tribe.CHECK_CHIEF)
    assert not tribe.is_same_check("Grug — Guard", tribe.CHECK_CHIEF)


def test_primary_maps_legacy_back():
    assert tribe.primary_check_name("Grug — Definition of Ready") == tribe.CHECK_CHIEF
    assert tribe.primary_check_name("Grug — Code Review") == tribe.CHECK_ELDER
    assert tribe.primary_check_name("Grug — Elder") == tribe.CHECK_ELDER


def test_registry_matches_modules():
    from personas.code_reviewer.dispatch import _CHECK_NAME as elder
    from personas.tpm.persona import _CHECK_NAME as chief
    from personas.guard.dispatch import _CHECK_NAME as guard
    assert elder == tribe.CHECK_ELDER
    assert chief == tribe.CHECK_CHIEF
    assert guard == tribe.CHECK_GUARD
