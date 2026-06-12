"""Tests for admin allowlist + role mgmt (Slice 8 #29).

Post-#354 swap: runs against the REAL Postgres test database via the
shared `pg_store` fixture (per feedback_no_coding_by_analogy — verify
against ground truth; the moto-DDB harness lives in git history).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def _adm(pg_store):
    """Yields the admin module with the store pointed at the test DB."""
    import admin

    yield admin


def _put_row(pk: str, attrs: dict) -> None:
    """Seed a raw META row through the adapter's own codec — the same
    write shape record_installation/upsert_oauth_user produce."""
    from adapters import pg_base

    with pg_base.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO grug_kv (pk, sk, data) VALUES (%s, 'META', %s) "
            "ON CONFLICT (pk, sk) DO UPDATE SET data = EXCLUDED.data",
            (pk, pg_base.encode_attrs(attrs)),
        )


def _seed_user(github_user_id, login="evan", role="admin", allowlisted=True):
    _put_row(
        f"USER#{github_user_id}",
        {
            "login": login,
            "role": role,
            "tier": "lifetime",
            "allowlisted": allowlisted,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _admin_user(github_user_id="100", login="admin"):
    from adapters.user_store import UserIdentity
    return UserIdentity(
        github_user_id=github_user_id, login=login, role="admin",
        tier="lifetime", allowlisted=True,
        created_at="", allowlisted_at=None, allowlisted_by=None,
    )


def test_list_users_returns_only_user_rows(_adm):
    _seed_user("1", "alice", "user")
    _seed_user("2", "bob", "user")
    _put_row("INST#42", {"account_login": "evan"})  # noise

    out = _adm.list_users(_=_admin_user())
    logins = sorted(u["login"] for u in out["users"])
    assert logins == ["alice", "bob"]


def test_list_users_excludes_oauth_blob(_adm):
    _seed_user("1", "alice")
    # Inject a fake oauth blob (bytes round-trip through the adapter's
    # b64 sentinel); admin response must NOT include it.
    _put_row(
        "USER#1",
        {"login": "alice", "role": "user", "tier": "lifetime",
         "allowlisted": True, "oauth_access_token_blob": b"fake-ciphertext"},
    )
    out = _adm.list_users(_=_admin_user())
    user = out["users"][0]
    assert "oauth_access_token_blob" not in user
    assert "oauth_access_token" not in user


def test_list_installations(_adm):
    _put_row(
        "INST#42",
        {"account_login": "evan", "account_type": "User",
         "installed_by_user_id": "100", "installed_at": "now"},
    )
    out = _adm.list_all_installations(_=_admin_user())
    assert out["installations"][0]["install_id"] == 42


def test_patch_user_flips_allowlist(_adm):
    from admin import UserPatchPayload
    _seed_user("1", "alice", "user", allowlisted=False)
    out = _adm.patch_user("1", UserPatchPayload(allowlisted=True), actor=_admin_user())
    assert out["before"]["allowlisted"] is False
    assert out["after"]["allowlisted"] is True
    assert out["changed"] is True
    # allowlisted_by records the immutable github_user_id, not login.
    # _admin_user() in this fixture is github_user_id="100".
    assert out["user"]["allowlisted_by"] == "100"


def test_patch_user_404_unknown(_adm):
    from admin import UserPatchPayload
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _adm.patch_user("999", UserPatchPayload(allowlisted=True), actor=_admin_user())
    assert e.value.status_code == 404


def test_patch_user_invalid_role_rejected(_adm):
    from admin import UserPatchPayload
    from fastapi import HTTPException
    _seed_user("1", "alice", "user")
    with pytest.raises(HTTPException) as e:
        _adm.patch_user("1", UserPatchPayload(role="superadmin"), actor=_admin_user())
    assert e.value.status_code == 400


def test_patch_user_invalid_tier_rejected(_adm):
    from admin import UserPatchPayload
    from fastapi import HTTPException
    _seed_user("1", "alice", "user")
    with pytest.raises(HTTPException) as e:
        _adm.patch_user("1", UserPatchPayload(tier="enterprise"), actor=_admin_user())
    assert e.value.status_code == 400


def test_patch_user_self_demotion_blocked(_adm):
    """Admin cannot demote themselves — prevents only-admin lock-out."""
    from admin import UserPatchPayload
    from fastapi import HTTPException
    _seed_user("100", "admin", "admin")
    with pytest.raises(HTTPException) as e:
        _adm.patch_user("100", UserPatchPayload(role="user"), actor=_admin_user("100"))
    assert e.value.status_code == 400 and "demote yourself" in e.value.detail


def test_patch_user_role_change_persists(_adm):
    """Regression from the DDB era (`role` was a reserved word needing an
    ExpressionAttributeNames alias); the invariant — a role change must
    actually persist — is backend-neutral."""
    from admin import UserPatchPayload
    _seed_user("1", "alice", "user")
    out = _adm.patch_user("1", UserPatchPayload(role="admin"), actor=_admin_user())
    assert out["after"]["role"] == "admin"
    from adapters.user_store import get_user_item
    assert get_user_item("1")["role"] == "admin"


def test_patch_user_no_op_payload_returns_unchanged(_adm):
    from admin import UserPatchPayload
    _seed_user("1", "alice", "user")
    out = _adm.patch_user("1", UserPatchPayload(), actor=_admin_user())
    assert out["changed"] is False


def test_patch_user_first_allowlist_writes_audit_trail(_adm):
    """First flip-to-allowlisted=True writes allowlisted_at +
    allowlisted_by audit fields. Without these we can't show a
    moderation log in admin UI."""
    from admin import UserPatchPayload
    _seed_user("1", "alice", "user", allowlisted=False)
    actor = _admin_user("100", "admin")

    out = _adm.patch_user("1", UserPatchPayload(allowlisted=True), actor=actor)
    assert out["changed"] is True
    from adapters.user_store import get_user_item
    row = get_user_item("1")
    assert row["allowlisted"] is True
    assert row.get("allowlisted_at"), "allowlisted_at missing on first flip"
    assert row.get("allowlisted_by") == "100", \
        "allowlisted_by must record the actor's github_user_id"


def test_scan_all_returns_every_row_for_prefix(_adm):
    """The DDB-era _scan_all capped pagination at 50 pages; its Postgres
    replacement is an unbounded indexed SELECT. Seed past where a paging
    bug would bite and assert nothing is dropped + prefixes don't bleed."""
    for i in range(60):
        _seed_user(str(i), f"user{i}", "user")
    _put_row("INST#7", {"account_login": "noise"})

    from admin import _scan_all
    items = _scan_all(pk_prefix="USER#")
    assert len(items) == 60
    assert all(it["PK"].startswith("USER#") for it in items)
