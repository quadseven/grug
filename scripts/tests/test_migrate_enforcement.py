"""Tests for the enforcement migration script.

Uses moto for DDB and unittest.mock for GitHub API calls.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import boto3
import moto
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import migrate_enforcement as mod


@pytest.fixture
def _ddb_table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("GRUG_DDB_TABLE", "grug-test")

    with moto.mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="grug-test",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table = ddb.Table("grug-test")
        table.put_item(Item={
            "PK": "INST#100", "SK": "META",
            "account_login": "githumps", "account_type": "User",
            "installed_by_user_id": "59060157",
        })
        table.put_item(Item={
            "PK": "INST#100", "SK": "REPO#7777",
            "tpm_enabled": True, "repo_full_name": "githumps/infra",
        })
        table.put_item(Item={
            "PK": "INST#100", "SK": "REPO#8888",
            "tpm_enabled": True, "repo_full_name": "githumps/grug",
        })
        table.put_item(Item={
            "PK": "INST#100", "SK": "REPO#9999",
            "tpm_enabled": False, "repo_full_name": "githumps/disabled-repo",
        })
        yield table


def _mock_repos():
    return [
        {"id": 7777, "full_name": "githumps/infra", "name": "infra",
         "owner": {"login": "githumps"}, "default_branch": "main"},
        {"id": 8888, "full_name": "githumps/grug", "name": "grug",
         "owner": {"login": "githumps"}, "default_branch": "main"},
        {"id": 9999, "full_name": "githumps/disabled-repo", "name": "disabled-repo",
         "owner": {"login": "githumps"}, "default_branch": "main"},
    ]


def test_dry_run_does_not_create_rulesets(_ddb_table, capsys, monkeypatch):
    """--dry-run emits what would happen without mutation."""
    monkeypatch.setenv("GITHUB_APP_ID_SSM", "/test/app-id")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_SSM", "/test/key")

    with patch.object(mod._TokenManager, "get_install_token", return_value="tok"), \
         patch.object(mod, "_list_install_repos", return_value=_mock_repos()), \
         patch.object(mod, "_detect_enforcement", return_value="none"), \
         patch.object(mod, "_create_ruleset") as mock_create:
        mod.migrate(dry_run=True)

    mock_create.assert_not_called()
    output = capsys.readouterr().out
    lines = [json.loads(l) for l in output.strip().split("\n")]
    actions = [l["action"] for l in lines]
    assert "dry_run_would_create" in actions
    assert "migration_complete" in actions


def test_creates_rulesets_for_unenforced_repos(_ddb_table, capsys, monkeypatch):
    """Repos with state=none get a ruleset created."""
    monkeypatch.setenv("GITHUB_APP_ID_SSM", "/test/app-id")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_SSM", "/test/key")

    with patch.object(mod._TokenManager, "get_install_token", return_value="tok"), \
         patch.object(mod, "_list_install_repos", return_value=_mock_repos()), \
         patch.object(mod, "_detect_enforcement", return_value="none"), \
         patch.object(mod, "_create_ruleset", return_value={"id": 42}) as mock_create, \
         patch.object(mod, "_set_enforcement_id") as mock_set:
        mod.migrate(dry_run=False)

    assert mock_create.call_count == 3
    assert mock_set.call_count == 3


def test_skips_externally_enforced_repos(_ddb_table, capsys, monkeypatch):
    """Repos with state=external are skipped (no ruleset created)."""
    monkeypatch.setenv("GITHUB_APP_ID_SSM", "/test/app-id")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_SSM", "/test/key")

    with patch.object(mod._TokenManager, "get_install_token", return_value="tok"), \
         patch.object(mod, "_list_install_repos", return_value=_mock_repos()), \
         patch.object(mod, "_detect_enforcement", return_value="external"), \
         patch.object(mod, "_create_ruleset") as mock_create:
        mod.migrate(dry_run=False)

    output = capsys.readouterr().out
    lines = [json.loads(l) for l in output.strip().split("\n")]
    skip_actions = [l for l in lines if l["action"] == "skip" and l.get("reason") == "external"]
    assert len(skip_actions) >= 2
    # grug repo is special-cased separately (legacy_bp_migration_candidate)


def test_skips_already_grug_managed(_ddb_table, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID_SSM", "/test/app-id")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_SSM", "/test/key")

    with patch.object(mod._TokenManager, "get_install_token", return_value="tok"), \
         patch.object(mod, "_list_install_repos", return_value=_mock_repos()), \
         patch.object(mod, "_detect_enforcement", return_value="grug_managed"), \
         patch.object(mod, "_create_ruleset") as mock_create:
        mod.migrate(dry_run=False)

    mock_create.assert_not_called()


def test_grug_repo_legacy_migration(_ddb_table, capsys, monkeypatch):
    """githumps/grug with external enforcement triggers legacy BP migration."""
    monkeypatch.setenv("GITHUB_APP_ID_SSM", "/test/app-id")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_SSM", "/test/key")

    def _side_effect_detect(token, owner, repo, branch):
        if repo == "grug":
            return "external"
        return "none"

    with patch.object(mod._TokenManager, "get_install_token", return_value="tok"), \
         patch.object(mod, "_list_install_repos", return_value=_mock_repos()), \
         patch.object(mod, "_detect_enforcement", side_effect=_side_effect_detect), \
         patch.object(mod, "_create_ruleset", return_value={"id": 55}) as mock_create, \
         patch.object(mod, "_remove_check_from_legacy_bp", return_value=True) as mock_rm, \
         patch.object(mod, "_set_enforcement_id") as mock_set:
        mod.migrate(dry_run=False)

    grug_calls = [c for c in mock_create.call_args_list if c.args[2] == "grug"]
    assert len(grug_calls) == 1
    mock_rm.assert_called_once()

    output = capsys.readouterr().out
    lines = [json.loads(l) for l in output.strip().split("\n")]
    assert any(l["action"] == "legacy_bp_check_removed" for l in lines)


def test_idempotent_rerun_skips_grug_managed(_ddb_table, capsys, monkeypatch):
    """Re-running after successful migration is a no-op."""
    monkeypatch.setenv("GITHUB_APP_ID_SSM", "/test/app-id")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_SSM", "/test/key")

    with patch.object(mod._TokenManager, "get_install_token", return_value="tok"), \
         patch.object(mod, "_list_install_repos", return_value=_mock_repos()), \
         patch.object(mod, "_detect_enforcement", return_value="grug_managed"), \
         patch.object(mod, "_create_ruleset") as mock_create:
        mod.migrate(dry_run=False)
        mod.migrate(dry_run=False)

    mock_create.assert_not_called()


def test_token_failure_logs_and_continues(_ddb_table, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID_SSM", "/test/app-id")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_SSM", "/test/key")

    with patch.object(mod._TokenManager, "get_install_token", side_effect=Exception("auth failed")):
        mod.migrate(dry_run=False)

    output = capsys.readouterr().out
    lines = [json.loads(l) for l in output.strip().split("\n")]
    assert any(l["action"] == "token_failed" for l in lines)
    complete = [l for l in lines if l["action"] == "migration_complete"][0]
    assert complete["stats"]["errors"] >= 1
