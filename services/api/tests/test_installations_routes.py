"""Tests for installations.py route handlers + auth helpers.

Covers:
- _ensure_can_access: admin always passes
- _ensure_can_access: install owner passes
- _ensure_can_access: stranger raises 403
- _ensure_can_access: int-vs-string user_id comparison robust
- list_installations: returns user's installs from GSI1 query
- list_installations: empty for user with no installs
- RepoConfigPayload: tpm_enabled defaults True
- RepoConfigPayload: explicit false validates
"""

from __future__ import annotations

import boto3
import pytest
from fastapi import HTTPException


@pytest.fixture
def _mod(monkeypatch):
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
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )
        import importlib
        import adapters.install_store as ins
        importlib.reload(ins)
        import installations as inst
        importlib.reload(inst)
        yield inst


def _user(user_id="100", role="user"):
    from adapters.user_store import User
    return User(
        github_user_id=user_id, login="evan", role=role, tier="free",
        allowlisted=True, oauth_access_token="x", oauth_refresh_token=None,
        created_at="",
    )


def test_ensure_can_access_admin_always_passes(_mod):
    install = {"installed_by_user_id": "999"}
    user = _user(user_id="100", role="admin")
    _mod._ensure_can_access(install, user)


def test_ensure_can_access_install_owner_passes(_mod):
    install = {"installed_by_user_id": "100"}
    user = _user(user_id="100", role="user")
    _mod._ensure_can_access(install, user)


def test_ensure_can_access_stranger_raises_403(_mod):
    install = {"installed_by_user_id": "999"}
    user = _user(user_id="100", role="user")
    with pytest.raises(HTTPException) as exc:
        _mod._ensure_can_access(install, user)
    assert exc.value.status_code == 403
    assert "not your installation" in exc.value.detail


def test_ensure_can_access_int_string_robust(_mod):
    """installed_by_user_id may be stored as int OR str depending on
    DDB type-coercion path. Comparison must treat them as equivalent."""
    install = {"installed_by_user_id": 100}  # int
    user = _user(user_id="100", role="user")  # str
    _mod._ensure_can_access(install, user)


def test_list_installations_returns_user_installs(_mod):
    from adapters.install_store import record_installation
    record_installation(
        install_id=1001, account_login="myorg", account_type="Organization",
        installed_by_user_id=100,
    )
    record_installation(
        install_id=1002, account_login="otherorg", account_type="Organization",
        installed_by_user_id=999,
    )
    user = _user(user_id="100")
    out = _mod.list_installations(user)
    install_ids = sorted(i["install_id"] for i in out["installations"])
    assert install_ids == [1001]


def test_list_installations_empty_for_user_with_none(_mod):
    user = _user(user_id="100")
    out = _mod.list_installations(user)
    assert out == {"installations": []}


def test_repo_config_payload_default_tpm_enabled_true(_mod):
    p = _mod.RepoConfigPayload()
    assert p.tpm_enabled is True


def test_repo_config_payload_explicit_false(_mod):
    p = _mod.RepoConfigPayload(tpm_enabled=False)
    assert p.tpm_enabled is False


def test_list_installations_skips_corrupt_pk_rows(_mod):
    """silent-failure-hunter P2 #6 regression: corrupt GSI1 row PK
    must skip + log, not crash entire endpoint."""
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("grug-main-test")
    # Good row + corrupt-PK row both indexed under GSI1PK=100
    table.put_item(Item={
        "PK": "INST#1001", "SK": "META",
        "account_login": "good", "account_type": "User",
        "installed_at": "2026-01-01T00:00:00Z",
        "installed_by_user_id": "100",
        "GSI1PK": "100", "GSI1SK": "INST#1001",
    })
    table.put_item(Item={
        "PK": "garbage-no-hash",
        "SK": "META",
        "account_login": "corrupt", "account_type": "User",
        "installed_by_user_id": "100",
        "GSI1PK": "100", "GSI1SK": "INST#bad",
    })
    user = _user(user_id="100")
    out = _mod.list_installations(user)
    install_ids = sorted(i["install_id"] for i in out["installations"])
    assert install_ids == [1001]  # corrupt row skipped, dashboard not blank
