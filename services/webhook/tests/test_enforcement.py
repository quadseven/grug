"""Tests for enforcement lifecycle — ensure/remove/heal enforcement.

Covers the enable → create → disable → delete lifecycle, idempotent
skip on existing enforcement, DDB persistence of ruleset_id,
self-healing on external delete, and best-effort error handling.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from enforcement import (
    ensure_enforcement,
    heal_enforcement,
    remove_enforcement,
    GRUG_TPM_RULESET_NAME,
    GRUG_DOR_CHECK_NAME,
)


# ── ensure_enforcement ───────────────────────────────────────────────

def test_ensure_creates_ruleset_when_none():
    """No enforcement → create ruleset + store ID in DDB."""
    with patch("enforcement.detect_enforcement", return_value="none") as mock_detect, \
         patch("enforcement.create_ruleset", return_value={"id": 42}) as mock_create, \
         patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        result = ensure_enforcement("tok", "myorg", "myrepo", "main", 100, 200)

    assert result == "grug_managed"
    mock_detect.assert_called_once_with(
        "tok", "myorg", "myrepo", "main", GRUG_DOR_CHECK_NAME, stored_ruleset_id=None,
    )
    mock_create.assert_called_once_with(
        "tok", "myorg", "myrepo", GRUG_TPM_RULESET_NAME, [GRUG_DOR_CHECK_NAME],
    )
    mock_set.assert_called_once_with(100, 200, 42)


def test_ensure_skips_when_grug_managed():
    """Already grug_managed → no-op."""
    with patch("enforcement.detect_enforcement", return_value="grug_managed"), \
         patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.create_ruleset") as mock_create:
        result = ensure_enforcement("tok", "o", "r", "main", 1, 2)

    assert result == "grug_managed"
    mock_create.assert_not_called()


def test_ensure_skips_when_external():
    """External enforcement → no-op (don't duplicate)."""
    with patch("enforcement.detect_enforcement", return_value="external"), \
         patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.create_ruleset") as mock_create:
        result = ensure_enforcement("tok", "o", "r", "main", 1, 2)

    assert result == "external"
    mock_create.assert_not_called()


def test_ensure_stores_ruleset_id_from_create_response():
    """Ruleset ID from GitHub's create response is persisted in DDB."""
    with patch("enforcement.detect_enforcement", return_value="none"), \
         patch("enforcement.create_ruleset", return_value={"id": 777}), \
         patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        ensure_enforcement("tok", "o", "r", "main", 10, 20)

    mock_set.assert_called_once_with(10, 20, 777)


def test_ensure_passes_stored_id_to_detect():
    """A previously-stored ruleset_id is threaded through to detect_enforcement.

    This is the ID-based detection path (grug#565):
    detect_enforcement matches by ID first, falling back to the
    Grug-prefix name heuristic only when nothing is on file yet.
    """
    with patch("enforcement.detect_enforcement", return_value="grug_managed") as mock_detect, \
         patch("adapters.install_store.get_enforcement_id", return_value=555), \
         patch("enforcement.migrate_check_context", return_value=False), \
         patch("enforcement.create_ruleset") as mock_create:
        result = ensure_enforcement("tok", "o", "r", "main", 1, 2)

    assert result == "grug_managed"
    mock_detect.assert_called_once_with(
        "tok", "o", "r", "main", GRUG_DOR_CHECK_NAME, stored_ruleset_id=555,
    )
    mock_create.assert_not_called()


def test_ensure_heals_stale_check_context_when_grug_managed():
    """grug_managed + a stored ruleset ID → self-heal a stale check
    context (e.g. a pre-rename em-dash title) via migrate_check_context."""
    with patch("enforcement.detect_enforcement", return_value="grug_managed"), \
         patch("adapters.install_store.get_enforcement_id", return_value=555), \
         patch("enforcement.migrate_check_context", return_value=True) as mock_migrate, \
         patch("enforcement.create_ruleset") as mock_create:
        result = ensure_enforcement("tok", "o", "r", "main", 1, 2)

    assert result == "grug_managed"
    mock_migrate.assert_called_once_with("tok", "o", "r", 555)
    mock_create.assert_not_called()


def test_ensure_skips_heal_when_no_stored_ruleset_id():
    """grug_managed but no stored ID (name-heuristic match only) →
    nothing to heal, migrate_check_context is not even attempted."""
    with patch("enforcement.detect_enforcement", return_value="grug_managed"), \
         patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.migrate_check_context") as mock_migrate:
        result = ensure_enforcement("tok", "o", "r", "main", 1, 2)

    assert result == "grug_managed"
    mock_migrate.assert_not_called()


def test_ensure_heal_failure_is_best_effort_and_does_not_raise():
    """A heal failure (network blip, 403, whatever) must never break the
    existing-enforcement return - it's cutover insurance, not a gate."""
    with patch("enforcement.detect_enforcement", return_value="grug_managed"), \
         patch("adapters.install_store.get_enforcement_id", return_value=555), \
         patch("enforcement.migrate_check_context", side_effect=RuntimeError("boom")):
        result = ensure_enforcement("tok", "o", "r", "main", 1, 2)

    assert result == "grug_managed"


def test_ensure_external_state_never_attempts_heal():
    """External enforcement isn't Grug's ruleset to touch - no heal call."""
    with patch("enforcement.detect_enforcement", return_value="external"), \
         patch("adapters.install_store.get_enforcement_id", return_value=555), \
         patch("enforcement.migrate_check_context") as mock_migrate, \
         patch("enforcement.create_ruleset"):
        result = ensure_enforcement("tok", "o", "r", "main", 1, 2)

    assert result == "external"
    mock_migrate.assert_not_called()


# ── migrate_check_context ───────────────────────────────────────────

def test_migrate_check_context_updates_stale_legacy_context():
    """A ruleset still requiring the pre-rename em-dash title gets PUT
    with the canonical check name."""
    from enforcement import migrate_check_context
    stale_ruleset = {
        "rules": [{
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "Grug — Definition of Ready"}]},
        }],
    }
    with patch("enforcement.get_ruleset", return_value=stale_ruleset), \
         patch("enforcement.update_ruleset") as mock_update:
        changed = migrate_check_context("tok", "o", "r", 555)

    assert changed is True
    mock_update.assert_called_once_with("tok", "o", "r", 555, [GRUG_DOR_CHECK_NAME])


def test_migrate_check_context_noop_when_already_canonical():
    """Already-canonical context → no PUT, returns False."""
    from enforcement import migrate_check_context
    canonical_ruleset = {
        "rules": [{
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": GRUG_DOR_CHECK_NAME}]},
        }],
    }
    with patch("enforcement.get_ruleset", return_value=canonical_ruleset), \
         patch("enforcement.update_ruleset") as mock_update:
        changed = migrate_check_context("tok", "o", "r", 555)

    assert changed is False
    mock_update.assert_not_called()


def test_migrate_check_context_noop_when_no_required_status_checks_rule():
    """A ruleset with no required_status_checks rule at all → nothing to heal."""
    from enforcement import migrate_check_context
    empty_ruleset = {"rules": [{"type": "creation"}]}
    with patch("enforcement.get_ruleset", return_value=empty_ruleset), \
         patch("enforcement.update_ruleset") as mock_update:
        changed = migrate_check_context("tok", "o", "r", 555)

    assert changed is False
    mock_update.assert_not_called()


# ── remove_enforcement ───────────────────────────────────────────────

def test_remove_deletes_by_stored_id():
    """Stored ruleset_id → delete it (plus any exact-name matches; none here)."""
    with patch("adapters.install_store.get_enforcement_id", return_value=42), \
         patch("enforcement.list_rulesets", return_value=[{"id": 42, "name": "Grug - Chief Enforcement"}]), \
         patch("enforcement.delete_ruleset") as mock_del, \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        remove_enforcement("tok", "myorg", "myrepo", 100, 200)

    mock_del.assert_called_once_with("tok", "myorg", "myrepo", 42)
    mock_set.assert_called_once_with(100, 200, None)


def test_remove_stored_id_plus_coexisting_legacy_deletes_both():
    """A stored canonical ID AND a coexisting legacy 'Grug — ...' ruleset:
    delete BOTH, or the legacy stays an orphaned merge gate while the store
    reports enforcement removed (found by CodeRabbit + Qodo on this PR)."""
    rulesets = [
        {"id": 42, "name": "Grug - Chief Enforcement"},       # the stored one
        {"id": 77, "name": "Grug — Chief Enforcement"},       # coexisting legacy
        {"id": 50, "name": "CI Required"},
    ]
    with patch("adapters.install_store.get_enforcement_id", return_value=42), \
         patch("enforcement.list_rulesets", return_value=rulesets), \
         patch("enforcement.delete_ruleset") as mock_del, \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        remove_enforcement("tok", "o", "r", 1, 2)

    assert mock_del.call_count == 2
    assert {c.args[3] for c in mock_del.call_args_list} == {42, 77}
    mock_set.assert_called_once_with(1, 2, None)


def test_remove_stored_id_survives_list_rulesets_failure():
    """A transient list_rulesets failure must NOT block deleting a known
    stored ID - the name scan is a best-effort supplement, not a gate."""
    with patch("adapters.install_store.get_enforcement_id", return_value=42), \
         patch("enforcement.list_rulesets", side_effect=RuntimeError("GitHub 503")), \
         patch("enforcement.delete_ruleset") as mock_del, \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        remove_enforcement("tok", "o", "r", 1, 2)

    mock_del.assert_called_once_with("tok", "o", "r", 42)
    mock_set.assert_called_once_with(1, 2, None)


def test_remove_no_stored_id_reraises_on_list_failure():
    """No stored ID AND listing fails -> nothing safe to delete; surface the
    error so the caller retries rather than silently reporting removed."""
    with patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.list_rulesets", side_effect=RuntimeError("GitHub 503")), \
         patch("enforcement.delete_ruleset") as mock_del:
        with pytest.raises(RuntimeError, match="GitHub 503"):
            remove_enforcement("tok", "o", "r", 1, 2)
    mock_del.assert_not_called()


def test_remove_falls_back_to_list_when_no_stored_id():
    """No stored ID → list rulesets, find by prefix, delete."""
    rulesets = [
        {"id": 99, "name": "Grug - Chief Enforcement"},
        {"id": 50, "name": "CI Required"},
    ]
    with patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.list_rulesets", return_value=rulesets), \
         patch("enforcement.delete_ruleset") as mock_del, \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        remove_enforcement("tok", "o", "r", 1, 2)

    mock_del.assert_called_once_with("tok", "o", "r", 99)
    mock_set.assert_called_once_with(1, 2, None)


def test_remove_fallback_still_finds_legacy_emdash_prefix():
    """Cutover: existing 'Grug \u2014 ...' rulesets must stay deletable via
    the prefix fallback - both prefixes remain supported."""
    rulesets = [
        {"id": 77, "name": "Grug \u2014 Chief Enforcement"},
        {"id": 50, "name": "CI Required"},
    ]
    with patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.list_rulesets", return_value=rulesets), \
         patch("enforcement.delete_ruleset") as mock_del, \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        remove_enforcement("tok", "o", "r", 1, 2)

    mock_del.assert_called_once_with("tok", "o", "r", 77)
    mock_set.assert_called_once_with(1, 2, None)


def test_remove_deletes_all_matching_rulesets_during_cutover():
    """No stored ID + BOTH canonical and legacy Grug rulesets present:
    delete every match, not just the first, or the second stays active +
    orphaned after the store is cleared."""
    rulesets = [
        {"id": 99, "name": "Grug - Chief Enforcement"},
        {"id": 77, "name": "Grug — Chief Enforcement"},
        {"id": 50, "name": "CI Required"},
    ]
    with patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.list_rulesets", return_value=rulesets), \
         patch("enforcement.delete_ruleset") as mock_del, \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        remove_enforcement("tok", "o", "r", 1, 2)

    assert mock_del.call_count == 2
    deleted_ids = {c.args[3] for c in mock_del.call_args_list}
    assert deleted_ids == {99, 77}
    mock_set.assert_called_once_with(1, 2, None)


def test_remove_fallback_ignores_unrelated_prefixed_user_ruleset():
    """A user ruleset that merely SHARES the 'Grug - ' prefix but is not one
    of grug's own enforcement names must NOT be deleted by the fallback."""
    rulesets = [
        {"id": 88, "name": "Grug - my custom branch rules"},
        {"id": 99, "name": "Grug - Chief Enforcement"},
    ]
    with patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.list_rulesets", return_value=rulesets), \
         patch("enforcement.delete_ruleset") as mock_del, \
         patch("adapters.install_store.set_enforcement_id"):
        remove_enforcement("tok", "o", "r", 1, 2)

    # Only the real enforcement ruleset (99); the user's 88 is left alone.
    mock_del.assert_called_once_with("tok", "o", "r", 99)


def test_remove_noop_when_nothing_exists():
    """No stored ID, no Grug-prefixed rulesets → nothing to do."""
    with patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.list_rulesets", return_value=[{"id": 1, "name": "CI"}]), \
         patch("enforcement.delete_ruleset") as mock_del:
        remove_enforcement("tok", "o", "r", 1, 2)

    mock_del.assert_not_called()


def test_remove_noop_when_list_empty():
    """No stored ID, no rulesets at all → nothing to do."""
    with patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.list_rulesets", return_value=[]), \
         patch("enforcement.delete_ruleset") as mock_del:
        remove_enforcement("tok", "o", "r", 1, 2)

    mock_del.assert_not_called()


# ── lifecycle round-trip ─────────────────────────────────────────────

def test_enable_then_disable_lifecycle():
    """Full lifecycle: enable creates, disable deletes."""
    with patch("enforcement.detect_enforcement", return_value="none"), \
         patch("enforcement.create_ruleset", return_value={"id": 55}), \
         patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        ensure_enforcement("tok", "o", "r", "main", 1, 2)

    mock_set.assert_called_with(1, 2, 55)

    with patch("adapters.install_store.get_enforcement_id", return_value=55), \
         patch("enforcement.list_rulesets", return_value=[{"id": 55, "name": "Grug - Chief Enforcement"}]), \
         patch("enforcement.delete_ruleset") as mock_del, \
         patch("adapters.install_store.set_enforcement_id") as mock_set2:
        remove_enforcement("tok", "o", "r", 1, 2)

    mock_del.assert_called_once_with("tok", "o", "r", 55)
    mock_set2.assert_called_once_with(1, 2, None)


# ── constants ────────────────────────────────────────────────────────

def test_ruleset_name():
    assert GRUG_TPM_RULESET_NAME == "Grug - Chief Enforcement"


def test_check_name():
    assert GRUG_DOR_CHECK_NAME == "Grug - Chief"


# ── heal_enforcement ────────────────────────────────────────────────

def test_heal_clears_stale_id_and_recreates():
    """Deleted Grug ruleset → clear old ID → ensure creates a new one."""
    with patch("enforcement.detect_enforcement", return_value="none"), \
         patch("enforcement.create_ruleset", return_value={"id": 99}), \
         patch("adapters.install_store.get_enforcement_id", return_value=99), \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        result = heal_enforcement("tok", "o", "r", "main", 1, 2, old_ruleset_id=42)

    assert result == "grug_managed"
    calls = mock_set.call_args_list
    assert calls[0].args == (1, 2, None)
    assert calls[1].args == (1, 2, 99)


def test_heal_returns_new_state():
    """heal_enforcement returns the EnforcementState from ensure."""
    with patch("adapters.install_store.set_enforcement_id"), \
         patch("enforcement.detect_enforcement", return_value="none"), \
         patch("enforcement.create_ruleset", return_value={"id": 50}), \
         patch("adapters.install_store.get_enforcement_id", return_value=50):
        result = heal_enforcement("tok", "o", "r", "main", 1, 2, old_ruleset_id=10)

    assert result == "grug_managed"


def test_heal_noop_when_external_enforcement_exists():
    """If someone added an external ruleset before we heal, skip creation."""
    with patch("adapters.install_store.set_enforcement_id") as mock_set, \
         patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.detect_enforcement", return_value="external"), \
         patch("enforcement.create_ruleset") as mock_create:
        result = heal_enforcement("tok", "o", "r", "main", 1, 2, old_ruleset_id=42)

    assert result == "external"
    mock_create.assert_not_called()
