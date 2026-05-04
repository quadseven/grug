"""Pure-function tests for admin._user_to_admin_view + _inst_to_admin_view.

Critical: _user_to_admin_view must NOT include oauth_access_token_blob /
oauth_refresh_token_blob. This is the boundary between encrypted DDB
storage and JSON responses sent to the admin SPA. A future dev adding
`**item` would silently exfil ciphertext to logs/screenshots.
"""

from __future__ import annotations

import admin


def _user_row(**overrides):
    base = {
        "PK": "USER#100",
        "SK": "META",
        "login": "evan",
        "role": "user",
        "tier": "free",
        "allowlisted": False,
        "created_at": "2026-01-01T00:00:00Z",
        "oauth_access_token_blob": b"\x00ENCRYPTED",
        "oauth_refresh_token_blob": b"\x00ENCRYPTED-REFRESH",
    }
    base.update(overrides)
    return base


def test_user_view_extracts_id_from_pk():
    out = admin._user_to_admin_view(_user_row(**{"PK": "USER#42"}))
    assert out["github_user_id"] == "42"


def test_user_view_excludes_oauth_blobs():
    """Security invariant: encrypted token ciphertext must NEVER reach
    the admin JSON response. Even though it's encrypted at rest, an
    admin downloading the dashboard JSON shouldn't see the blob."""
    row = _user_row()
    out = admin._user_to_admin_view(row)
    # Hard-fail on any blob/token key bleeding through
    for k in out:
        assert "oauth" not in k.lower(), f"oauth key leaked: {k}"
        assert "blob" not in k.lower(), f"blob key leaked: {k}"
        assert "token" not in k.lower(), f"token key leaked: {k}"


def test_user_view_default_role_user():
    row = _user_row()
    row.pop("role", None)
    out = admin._user_to_admin_view(row)
    assert out["role"] == "user"


def test_user_view_default_tier_free():
    row = _user_row()
    row.pop("tier", None)
    out = admin._user_to_admin_view(row)
    assert out["tier"] == "free"


def test_user_view_allowlisted_truthy_coerces_bool():
    row = _user_row(allowlisted="yes")  # truthy non-bool from DDB
    out = admin._user_to_admin_view(row)
    assert out["allowlisted"] is True


def test_user_view_admin_role_passes_through():
    row = _user_row(role="admin", tier="lifetime", allowlisted=True)
    out = admin._user_to_admin_view(row)
    assert out["role"] == "admin"
    assert out["tier"] == "lifetime"
    assert out["allowlisted"] is True


def test_user_view_includes_allowlist_audit_fields():
    row = _user_row(
        allowlisted=True,
        allowlisted_at="2026-04-01T00:00:00Z",
        allowlisted_by="999",
    )
    out = admin._user_to_admin_view(row)
    assert out["allowlisted_at"] == "2026-04-01T00:00:00Z"
    assert out["allowlisted_by"] == "999"


def test_inst_view_extracts_install_id_from_pk():
    out = admin._inst_to_admin_view({
        "PK": "INST#1234",
        "account_login": "myorg", "account_type": "Organization",
        "installed_at": "2026-04-01T00:00:00Z",
        "installed_by_user_id": "100",
    })
    assert out["install_id"] == 1234
    assert out["account_login"] == "myorg"


def test_inst_view_default_account_type_user():
    out = admin._inst_to_admin_view({"PK": "INST#1"})
    assert out["account_type"] == "User"


def test_inst_view_int_install_id_round_trip():
    """install_id always emitted as int — SPA dashboards expect numeric."""
    out = admin._inst_to_admin_view({"PK": "INST#999"})
    assert isinstance(out["install_id"], int)
    assert out["install_id"] == 999
