"""Coverage for adapters.user_store.delete_user_state + corruption recovery.

Regression tests for the peer-review CRITICAL on PR #151:

1. `delete_user_state` must REMOVE only the credential blobs — never the
   identity row's role/tier/allowlisted/created_at fields. An encryption
   failure (KMS key drift, AAD mismatch) must not strip an admin's
   privileges or a paid user's tier.

2. `get_user_with_tokens` must return `None` on `CredentialBlobCorrupt`
   even if the subsequent `delete_user_state` purge itself raises (DB
   outage, pool exhaustion) — the corrupt blob is unrecoverable; the
   user is going to /signin regardless and the next `upsert_oauth_user`
   will overwrite the corrupt blob anyway. The fix must not turn a 401
   (re-auth required) into a 500 (broken service).

Post-#354 swap: runs against the REAL Postgres test database via the
shared `pg_store` fixture. Out-of-band admin writes go through the
store's own `update_user_fields`; raw reads through `get_user_item`.
"""

from __future__ import annotations

from unittest.mock import patch

import psycopg
import pytest


@pytest.fixture
def _us(pg_store):
    """Post-#354 swap: delegates to the shared real-Postgres fixture
    (conftest.pg_store) - moto-DDB setup lives in git history."""
    yield pg_store["user_store"]


def test_delete_user_state_preserves_admin_metadata(_us):
    """Spec 0005 PurgeCorrupt: credential blobs go, identity stays.
    A KMS key rotation must NOT strip admin role or paid tier."""
    _us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="ACCESS-1", oauth_refresh_token="REFRESH-1",
    )
    # Promote to admin / lifetime / allowlisted out-of-band.
    _us.update_user_fields(
        "100",
        {"role": "admin", "tier": "lifetime", "allowlisted": True,
         "allowlisted_by": "admin@grug.lol"},
    )

    _us.delete_user_state("100")

    # Credential blobs gone.
    item = _us.get_user_item("100")
    assert item is not None, "row was deleted entirely — admin/tier/allowlist destroyed"
    assert "oauth_access_token_blob" not in item, "access blob should be REMOVEd"
    assert "oauth_refresh_token_blob" not in item, "refresh blob should be REMOVEd"
    # Identity preserved.
    assert item["role"] == "admin"
    assert item["tier"] == "lifetime"
    assert item["allowlisted"] is True
    assert item["allowlisted_by"] == "admin@grug.lol"
    assert item["login"] == "evan"


def test_delete_user_state_is_idempotent_when_blobs_absent(_us):
    """Calling delete_user_state on a row that has no token blobs (e.g.
    a 2nd corruption-purge race) must not raise."""
    _us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="x", oauth_refresh_token=None,
    )
    _us.delete_user_state("100")
    _us.delete_user_state("100")  # second purge — no-op, must not raise


def test_get_user_with_tokens_returns_none_on_corruption(_us):
    """When decrypt raises CredentialBlobCorrupt, the function must
    purge the credential blobs and return None for clean /signin redirect."""
    _us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="ACCESS-1", oauth_refresh_token="REFRESH-1",
    )
    from crypto.kms_envelope import CredentialBlobCorrupt

    # decrypt_for_user is lazily imported inside get_user_with_tokens; patch at the source.
    with patch("crypto.kms_envelope.decrypt_for_user", side_effect=CredentialBlobCorrupt("test")):
        result = _us.get_user_with_tokens("100")

    assert result is None, "corruption must surface as None for /signin redirect"
    # Identity preserved, blobs gone.
    item = _us.get_user_item("100")
    assert item is not None
    assert "oauth_access_token_blob" not in item


def test_get_user_with_tokens_returns_none_even_when_purge_fails(_us):
    """If delete_user_state itself raises (DB outage, pool exhaustion),
    the original CredentialBlobCorrupt must NOT be masked — user must
    still reach /signin (None), not see a 500.

    The patch target is the IMPLEMENTATION module (pg_user_store), not
    the facade: get_user_with_tokens calls its own module-global
    delete_user_state, so patching the facade's re-export intercepts
    nothing and the test passes vacuously (audit #366 CRITICAL-1). The
    simulated failure is psycopg.Error — the class the purge guard
    actually catches."""
    _us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="ACCESS-1", oauth_refresh_token=None,
    )
    from crypto.kms_envelope import CredentialBlobCorrupt

    purge_calls = []

    def _purge_boom(user_id):
        purge_calls.append(user_id)
        raise psycopg.OperationalError("connection pool exhausted")

    with patch("crypto.kms_envelope.decrypt_for_user", side_effect=CredentialBlobCorrupt("test")):
        with patch("adapters.pg_user_store.delete_user_state", side_effect=_purge_boom):
            result = _us.get_user_with_tokens("100")

    assert purge_calls == ["100"], "purge must have been attempted (patch actually intercepted)"
    assert result is None, "purge failure must not mask the corruption — user still routes to /signin"


def test_upsert_oauth_user_admin_change_not_clobbered_by_oauth_refresh(_us):
    """Lost-update regression: after admin allowlists a user, a concurrent
    OAuth refresh that read the row PRE-allowlist must NOT overwrite the
    allowlisted=True back to False. The atomic jsonb-merge upsert preserves
    admin-side changes regardless of read ordering."""
    _us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="ACCESS-1", oauth_refresh_token="REFRESH-1",
    )
    # Admin flips allowlisted -> True (the "concurrent admin write").
    _us.update_user_fields(
        "100", {"role": "admin", "tier": "lifetime", "allowlisted": True}
    )
    # OAuth re-auth comes through (token rotation).
    _us.upsert_oauth_user(
        github_user_id="100", login="evan",
        oauth_access_token="ACCESS-2", oauth_refresh_token="REFRESH-2",
    )

    u = _us.get_user("100")
    assert u is not None
    assert u.role == "admin", "atomic update must preserve admin role across OAuth refresh"
    assert u.tier == "lifetime"
    assert u.allowlisted is True, "atomic update must preserve allowlist across OAuth refresh"
