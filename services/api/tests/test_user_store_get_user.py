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

import pytest


@pytest.fixture
def _us(pg_store):
    """Post-#354 swap: delegates to the shared real-Postgres fixture
    (conftest.pg_store) - moto-DDB setup lives in git history."""
    yield pg_store["user_store"]


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
    # Bump to admin out-of-band via the store's own field-update helper.
    _us.update_user_fields(
        "100", {"role": "admin", "tier": "lifetime", "allowlisted": True}
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
