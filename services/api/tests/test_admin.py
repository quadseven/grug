"""Tests for admin allowlist + role mgmt (Slice 8 #29).

Uses moto DDB so logic runs against real boto3 table shape (per
feedback_no_coding_by_analogy — verify against ground truth).
"""

from __future__ import annotations

from datetime import datetime, timezone

import boto3
import pytest


@pytest.fixture(autouse=True)
def _ddb(monkeypatch):
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
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        # Reload modules so module-scope `_table` picks up env var.
        import importlib
        import adapters.user_store as us
        importlib.reload(us)
        import admin as adm
        importlib.reload(adm)
        yield adm


def _seed_user(github_user_id, login="evan", role="admin", allowlisted=True):
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("grug-main-test")
    table.put_item(Item={
        "PK": f"USER#{github_user_id}",
        "SK": "META",
        "login": login,
        "role": role,
        "tier": "lifetime",
        "allowlisted": allowlisted,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def _admin_user(github_user_id="100", login="admin"):
    from adapters.user_store import User
    return User(
        github_user_id=github_user_id, login=login, role="admin",
        tier="lifetime", allowlisted=True, oauth_access_token="",
        oauth_refresh_token=None, created_at="", allowlisted_at=None,
        allowlisted_by=None,
    )


def test_list_users_returns_only_user_rows(_ddb):
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("grug-main-test")
    _seed_user("1", "alice", "user")
    _seed_user("2", "bob", "user")
    table.put_item(Item={"PK": "INST#42", "SK": "META", "account_login": "evan"})  # noise

    out = _ddb.list_users(_=_admin_user())
    logins = sorted(u["login"] for u in out["users"])
    assert logins == ["alice", "bob"]


def test_list_users_excludes_oauth_blob(_ddb):
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("grug-main-test")
    _seed_user("1", "alice")
    # Inject a fake oauth blob; admin response must NOT include it.
    table.update_item(
        Key={"PK": "USER#1", "SK": "META"},
        UpdateExpression="SET oauth_access_token_blob = :b",
        ExpressionAttributeValues={":b": b"fake-ciphertext"},
    )
    out = _ddb.list_users(_=_admin_user())
    user = out["users"][0]
    assert "oauth_access_token_blob" not in user
    assert "oauth_access_token" not in user


def test_list_installations(_ddb):
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("grug-main-test")
    table.put_item(Item={"PK": "INST#42", "SK": "META",
                         "account_login": "evan", "account_type": "User",
                         "installed_by_user_id": "100", "installed_at": "now"})
    out = _ddb.list_all_installations(_=_admin_user())
    assert out["installations"][0]["install_id"] == 42


def test_patch_user_flips_allowlist(_ddb):
    from admin import UserPatchPayload
    _seed_user("1", "alice", "user", allowlisted=False)
    out = _ddb.patch_user("1", UserPatchPayload(allowlisted=True), actor=_admin_user())
    assert out["before"]["allowlisted"] is False
    assert out["after"]["allowlisted"] is True
    assert out["changed"] is True
    assert out["user"]["allowlisted_by"] == "admin"


def test_patch_user_404_unknown(_ddb):
    from admin import UserPatchPayload
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _ddb.patch_user("999", UserPatchPayload(allowlisted=True), actor=_admin_user())
    assert e.value.status_code == 404


def test_patch_user_invalid_role_rejected(_ddb):
    from admin import UserPatchPayload
    from fastapi import HTTPException
    _seed_user("1", "alice", "user")
    with pytest.raises(HTTPException) as e:
        _ddb.patch_user("1", UserPatchPayload(role="superadmin"), actor=_admin_user())
    assert e.value.status_code == 400


def test_patch_user_invalid_tier_rejected(_ddb):
    from admin import UserPatchPayload
    from fastapi import HTTPException
    _seed_user("1", "alice", "user")
    with pytest.raises(HTTPException) as e:
        _ddb.patch_user("1", UserPatchPayload(tier="enterprise"), actor=_admin_user())
    assert e.value.status_code == 400


def test_patch_user_self_demotion_blocked(_ddb):
    """Admin cannot demote themselves — prevents only-admin lock-out."""
    from admin import UserPatchPayload
    from fastapi import HTTPException
    _seed_user("100", "admin", "admin")
    with pytest.raises(HTTPException) as e:
        _ddb.patch_user("100", UserPatchPayload(role="user"), actor=_admin_user("100"))
    assert e.value.status_code == 400 and "demote yourself" in e.value.detail


def test_patch_user_role_change_uses_reserved_word_alias(_ddb):
    """`role` is a DDB reserved word; must use ExpressionAttributeNames alias."""
    from admin import UserPatchPayload
    _seed_user("1", "alice", "user")
    out = _ddb.patch_user("1", UserPatchPayload(role="admin"), actor=_admin_user())
    assert out["after"]["role"] == "admin"


def test_patch_user_no_op_payload_returns_unchanged(_ddb):
    from admin import UserPatchPayload
    _seed_user("1", "alice", "user")
    out = _ddb.patch_user("1", UserPatchPayload(), actor=_admin_user())
    assert out["changed"] is False
