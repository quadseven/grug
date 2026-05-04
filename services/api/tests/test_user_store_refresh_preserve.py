"""Regression test for Sentry HIGH on PR #39.

GitHub OAuth re-auth can return an access token WITHOUT a new refresh
token (token rotation policy varies). The earlier upsert_oauth_user
unconditionally overwrote the row via put_item, silently dropping the
stored refresh blob in that case. Fix preserves the existing refresh
blob when no new one is provided.
"""

from __future__ import annotations

import boto3
import pytest


@pytest.fixture(autouse=True)
def _ddb_table(monkeypatch):
    moto = pytest.importorskip("moto")
    from moto import mock_aws  # type: ignore

    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("GRUG_DDB_TABLE", "grug-main-test")
        # KMS CMK for envelope encryption — moto provides a mock KMS.
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
        # Reload so module-scope env reads pick up the moto table name + CMK.
        import importlib
        import crypto.kms_envelope as kms_mod
        importlib.reload(kms_mod)
        import adapters.user_store as us
        importlib.reload(us)
        yield us


def test_re_auth_without_new_refresh_preserves_existing(_ddb_table):
    us = _ddb_table

    # First sign-in supplies BOTH access + refresh.
    u1 = us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="initial-access",
        oauth_refresh_token="initial-refresh",
    )
    assert u1.oauth_refresh_token == "initial-refresh"

    # Re-auth supplies access only — refresh from prior sign-in must remain.
    us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="rotated-access",
        oauth_refresh_token=None,
    )
    u2 = us.get_user("100")
    assert u2.oauth_access_token == "rotated-access"
    assert u2.oauth_refresh_token == "initial-refresh", \
        "refresh token wiped on re-auth without new refresh — Sentry HIGH PR #39"


def test_re_auth_with_new_refresh_replaces(_ddb_table):
    us = _ddb_table
    us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="a1", oauth_refresh_token="r1",
    )
    us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="a2", oauth_refresh_token="r2",
    )
    u = us.get_user("100")
    assert u.oauth_access_token == "a2"
    assert u.oauth_refresh_token == "r2"


def test_first_signin_with_no_refresh_works(_ddb_table):
    """Edge case: first OAuth grant supplies no refresh (some providers)."""
    us = _ddb_table
    u = us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="a1", oauth_refresh_token=None,
    )
    assert u.oauth_access_token == "a1"
    assert u.oauth_refresh_token is None

    fetched = us.get_user("100")
    assert fetched.oauth_refresh_token is None
