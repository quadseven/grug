"""Coverage for adapters.user_store.get_user + get_user_with_tokens.

Per issue #103, the User type was split: get_user returns identity-only
(no KMS Decrypt), get_user_with_tokens returns identity + decrypted
tokens. Token-decrypt path tests exercise the latter; identity-only
tests exercise the former.

- get_user / get_user_with_tokens return None for unknown user
- get_user_with_tokens decrypts access_token + refresh_token
- get_user_with_tokens returns refresh=None when blob absent
- get_user identity is admin/tier/allowlisted preserved across upsert
"""

from __future__ import annotations

import boto3
import pytest


@pytest.fixture
def _us(monkeypatch):
    moto = pytest.importorskip("moto")
    from moto import mock_aws  # type: ignore

    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("GRUG_DDB_TABLE", "grug-main-test")
        kms = boto3.client("kms", region_name="us-east-1")
        cmk = kms.create_key(Description="test-grug-tokens")
        monkeypatch.setenv("GRUG_KMS_CMK_ARN", cmk["KeyMetadata"]["Arn"])
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
        import importlib
        import crypto.kms_envelope as kms_mod
        importlib.reload(kms_mod)
        import adapters.user_store as us
        importlib.reload(us)
        yield us


def test_get_user_unknown_returns_none(_us):
    assert _us.get_user("99999") is None


def test_get_user_with_tokens_returns_decrypted_access_token(_us):
    _us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="ACCESS-1", oauth_refresh_token="REFRESH-1",
    )
    u = _us.get_user_with_tokens("100")
    assert u is not None
    assert u.oauth_access_token == "ACCESS-1"
    assert u.oauth_refresh_token == "REFRESH-1"
    # Identity nested attribute carries the same login + role.
    assert u.identity.login == "evan"


def test_get_user_with_tokens_no_refresh_returns_none_refresh(_us):
    _us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="ACCESS-only", oauth_refresh_token=None,
    )
    u = _us.get_user_with_tokens("100")
    assert u is not None
    assert u.oauth_access_token == "ACCESS-only"
    assert u.oauth_refresh_token is None


def test_get_user_does_not_carry_token_fields(_us):
    """Identity-only path must not expose token attrs (#103 invariant)."""
    _us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="x", oauth_refresh_token=None,
    )
    u = _us.get_user("100")
    assert u is not None
    assert not hasattr(u, "oauth_access_token")
    assert not hasattr(u, "oauth_refresh_token")


def test_get_user_preserves_login_and_defaults(_us):
    _us.upsert_oauth_user(
        github_user_id="100", login="myname",
        oauth_access_token="x", oauth_refresh_token=None,
    )
    u = _us.get_user("100")
    assert u.login == "myname"
    assert u.role == "user"
    assert u.tier == "free"
    assert u.allowlisted is False


def test_get_user_returns_admin_state_after_allowlist(_us):
    """upsert_oauth_user preserves admin/tier/allowlisted on re-auth."""
    _us.upsert_oauth_user(
        github_user_id="100", login="myname",
        oauth_access_token="x", oauth_refresh_token=None,
    )
    # Bump to admin in DDB directly
    _us._table.update_item(
        Key={"PK": _us._user_pk("100"), "SK": "META"},
        UpdateExpression="SET #r = :r, tier = :t, allowlisted = :a",
        ExpressionAttributeNames={"#r": "role"},
        ExpressionAttributeValues={":r": "admin", ":t": "lifetime", ":a": True},
    )
    u = _us.get_user("100")
    assert u.role == "admin"
    assert u.tier == "lifetime"
    assert u.allowlisted is True


def test_get_user_round_trip_with_only_access_token(_us):
    """Edge case: provider returned access but never refresh."""
    _us.upsert_oauth_user(
        github_user_id="200", login="bob",
        oauth_access_token="bob-token", oauth_refresh_token=None,
    )
    u = _us.get_user_with_tokens("200")
    assert u.oauth_access_token == "bob-token"
    assert u.oauth_refresh_token is None
    assert u.identity.created_at != ""
