"""Tests for enforcement lifecycle — ensure/remove/heal enforcement.

Covers the enable → create → disable → delete lifecycle, idempotent
skip on existing enforcement, DDB persistence of ruleset_id,
self-healing on external delete, and best-effort error handling.
"""

from __future__ import annotations

from unittest.mock import patch

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
         patch("enforcement.create_ruleset") as mock_create:
        result = ensure_enforcement("tok", "o", "r", "main", 1, 2)

    assert result == "grug_managed"
    mock_detect.assert_called_once_with(
        "tok", "o", "r", "main", GRUG_DOR_CHECK_NAME, stored_ruleset_id=555,
    )
    mock_create.assert_not_called()


# ── remove_enforcement ───────────────────────────────────────────────

def test_remove_deletes_by_stored_id():
    """Stored ruleset_id → delete directly, clear DDB."""
    with patch("adapters.install_store.get_enforcement_id", return_value=42), \
         patch("enforcement.delete_ruleset") as mock_del, \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        remove_enforcement("tok", "myorg", "myrepo", 100, 200)

    mock_del.assert_called_once_with("tok", "myorg", "myrepo", 42)
    mock_set.assert_called_once_with(100, 200, None)


def test_remove_falls_back_to_list_when_no_stored_id():
    """No stored ID → list rulesets, find by prefix, delete."""
    rulesets = [
        {"id": 99, "name": "Grug — Chief Enforcement"},
        {"id": 50, "name": "CI Required"},
    ]
    with patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("enforcement.list_rulesets", return_value=rulesets), \
         patch("enforcement.delete_ruleset") as mock_del, \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        remove_enforcement("tok", "o", "r", 1, 2)

    mock_del.assert_called_once_with("tok", "o", "r", 99)
    mock_set.assert_called_once_with(1, 2, None)


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
         patch("enforcement.delete_ruleset") as mock_del, \
         patch("adapters.install_store.set_enforcement_id") as mock_set2:
        remove_enforcement("tok", "o", "r", 1, 2)

    mock_del.assert_called_once_with("tok", "o", "r", 55)
    mock_set2.assert_called_once_with(1, 2, None)


# ── constants ────────────────────────────────────────────────────────

def test_ruleset_name():
    assert GRUG_TPM_RULESET_NAME == "Grug — Chief Enforcement"


def test_check_name():
    assert GRUG_DOR_CHECK_NAME == "Grug — Chief"


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
