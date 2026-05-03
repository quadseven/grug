"""Tests for install_store + allowlist gate.

Uses moto to spin a local DDB so adapter logic runs against a real
boto3 table — closer to prod than naive mock objects (matches
`feedback_no_coding_by_analogy` — verify against ground-truth shape).
"""

from __future__ import annotations

import os

import boto3
import pytest


@pytest.fixture(autouse=True)
def _ddb_table(monkeypatch):
    """Spin a moto DDB grug-main with the production schema."""
    moto = pytest.importorskip("moto")
    from moto import mock_aws  # type: ignore

    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("GRUG_DDB_TABLE", "grug-main-test")
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="grug-main-test",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        # Re-import after env-var set so module-scope _table picks up new name.
        import importlib
        import adapters.install_store as mod
        importlib.reload(mod)
        yield mod


def test_record_then_lookup(_ddb_table):
    mod = _ddb_table
    mod.record_installation(
        install_id=42, account_login="evan", account_type="User",
        installed_by_user_id=99,
    )
    inst = mod.get_installation(42)
    assert inst["account_login"] == "evan"
    assert inst["installed_by_user_id"] == "99"


def test_delete_installation(_ddb_table):
    mod = _ddb_table
    mod.record_installation(
        install_id=42, account_login="x", account_type="User",
        installed_by_user_id=1,
    )
    mod.delete_installation(42)
    assert mod.get_installation(42) is None


def test_allowlist_unknown_install_returns_false(_ddb_table):
    assert _ddb_table.is_install_allowlisted(404) is False


def test_allowlist_user_not_allowlisted_returns_false(_ddb_table):
    mod = _ddb_table
    mod.record_installation(
        install_id=10, account_login="bob", account_type="User",
        installed_by_user_id=200,
    )
    # USER#200 row missing entirely — should still return False, not crash.
    assert mod.is_install_allowlisted(10) is False


def test_allowlist_user_explicitly_false(_ddb_table):
    mod = _ddb_table
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("grug-main-test")
    table.put_item(Item={"PK": "USER#200", "SK": "META", "allowlisted": False})
    mod.record_installation(
        install_id=10, account_login="bob", account_type="User",
        installed_by_user_id=200,
    )
    assert mod.is_install_allowlisted(10) is False


def test_allowlist_user_true(_ddb_table):
    mod = _ddb_table
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("grug-main-test")
    table.put_item(Item={"PK": "USER#200", "SK": "META", "allowlisted": True})
    mod.record_installation(
        install_id=10, account_login="bob", account_type="User",
        installed_by_user_id=200,
    )
    assert mod.is_install_allowlisted(10) is True


def test_record_idempotent(_ddb_table):
    """Re-recording the same install must not error."""
    mod = _ddb_table
    for _ in range(3):
        mod.record_installation(
            install_id=10, account_login="bob", account_type="User",
            installed_by_user_id=200,
        )
    assert mod.get_installation(10)["account_login"] == "bob"
