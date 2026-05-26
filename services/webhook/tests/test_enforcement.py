"""Tests for enforcement lifecycle — ensure/remove enforcement.

Covers the enable → create → disable → delete lifecycle, idempotent
skip on existing enforcement, DDB persistence of ruleset_id, and
best-effort error handling.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock, call

import httpx
import pytest

from enforcement import (
    ensure_enforcement,
    remove_enforcement,
    GRUG_TPM_RULESET_NAME,
    GRUG_DOR_CHECK_NAME,
)


# ── helpers ──────────────────────────────────────────────────────────

def _ok_response(json_body=None, status_code=200):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=json_body if json_body is not None else {})
    return r


# ── ensure_enforcement ───────────────────────────────────────────────

def test_ensure_creates_ruleset_when_none():
    """No enforcement → create ruleset + store ID in DDB."""
    rulesets_resp = _ok_response([])
    legacy_resp = _ok_response({"contexts": []})
    create_resp = _ok_response({"id": 42}, 201)

    with patch("enforcement.detect_enforcement", return_value="none") as mock_detect, \
         patch("enforcement.create_ruleset", return_value={"id": 42}) as mock_create, \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        result = ensure_enforcement("tok", "myorg", "myrepo", "main", 100, 200)

    assert result == "grug_managed"
    mock_detect.assert_called_once_with("tok", "myorg", "myrepo", "main", GRUG_DOR_CHECK_NAME)
    mock_create.assert_called_once_with(
        "tok", "myorg", "myrepo", GRUG_TPM_RULESET_NAME, [GRUG_DOR_CHECK_NAME],
    )
    mock_set.assert_called_once_with(100, 200, 42)


def test_ensure_skips_when_grug_managed():
    """Already grug_managed → no-op."""
    with patch("enforcement.detect_enforcement", return_value="grug_managed"), \
         patch("enforcement.create_ruleset") as mock_create:
        result = ensure_enforcement("tok", "o", "r", "main", 1, 2)

    assert result == "grug_managed"
    mock_create.assert_not_called()


def test_ensure_skips_when_external():
    """External enforcement → no-op (don't duplicate)."""
    with patch("enforcement.detect_enforcement", return_value="external"), \
         patch("enforcement.create_ruleset") as mock_create:
        result = ensure_enforcement("tok", "o", "r", "main", 1, 2)

    assert result == "external"
    mock_create.assert_not_called()


def test_ensure_stores_ruleset_id_from_create_response():
    """Ruleset ID from GitHub's create response is persisted in DDB."""
    with patch("enforcement.detect_enforcement", return_value="none"), \
         patch("enforcement.create_ruleset", return_value={"id": 777}), \
         patch("adapters.install_store.set_enforcement_id") as mock_set:
        ensure_enforcement("tok", "o", "r", "main", 10, 20)

    mock_set.assert_called_once_with(10, 20, 777)


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
        {"id": 99, "name": "Grug — TPM Enforcement"},
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
    assert GRUG_TPM_RULESET_NAME == "Grug — TPM Enforcement"


def test_check_name():
    assert GRUG_DOR_CHECK_NAME == "Grug — Definition of Ready"
