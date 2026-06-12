"""Tests for installations.py route handlers + auth helpers.

Covers:
- _ensure_can_access: admin always passes
- _ensure_can_access: install owner passes
- _ensure_can_access: stranger raises 403
- _ensure_can_access: int-vs-string user_id comparison robust
- list_installations: returns user's installs from the gsi1pk-indexed lookup
- list_installations: empty for user with no installs
- RepoConfigPayload: tpm_enabled defaults True
- RepoConfigPayload: explicit false validates
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException


@pytest.fixture
def _mod(pg_store):
    """Post-#354 swap: delegates to the shared real-Postgres fixture."""
    import installations as inst_routes

    yield inst_routes


def _user(user_id="100", role="user"):
    from adapters.user_store import UserIdentity
    return UserIdentity(
        github_user_id=user_id, login="evan", role=role, tier="free",
        allowlisted=True, created_at="",
        allowlisted_at=None, allowlisted_by=None,
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
    which writer produced the row (jsonb preserves the caller's type;
    rows migrated from DDB may differ). Treat them as equivalent."""
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
    from tests.conftest import seed_meta

    # Good row + corrupt-PK row both indexed under gsi1pk=100
    seed_meta(
        "INST#1001",
        {"account_login": "good", "account_type": "User",
         "installed_at": "2026-01-01T00:00:00Z", "installed_by_user_id": "100"},
        gsi1pk="100", gsi1sk="INST#1001",
    )
    seed_meta(
        "garbage-no-hash",
        {"account_login": "corrupt", "account_type": "User",
         "installed_by_user_id": "100"},
        gsi1pk="100", gsi1sk="INST#bad",
    )
    user = _user(user_id="100")
    out = _mod.list_installations(user)
    install_ids = sorted(i["install_id"] for i in out["installations"])
    assert install_ids == [1001]  # corrupt row skipped, dashboard not blank


# ── get_enforcement resilient fallback (dashboard 429 storm) ──────────

def test_get_enforcement_fallback_grug_managed_when_stored(_mod, monkeypatch):
    """GitHub rate-limited live detection (httpx error after retries) + a
    stored ruleset id → degrade to grug_managed, never 500 / false 'none'."""
    import httpx
    inst = _mod
    monkeypatch.setattr(inst, "get_installation", lambda i: {"installed_by_user_id": "999"})
    req = httpx.Request("GET", "https://api.github.com/x")
    err = httpx.HTTPStatusError("rate limited", request=req, response=httpx.Response(429, request=req))
    monkeypatch.setattr(inst, "with_install_token_retry", lambda iid, fn: (_ for _ in ()).throw(err))
    monkeypatch.setattr(inst, "get_repo_config", lambda i, r: {"enforcement_ruleset_id": 555})

    out = inst.get_enforcement(1, 2, user=_user(role="admin"))
    assert out == {"repo_id": 2, "enforcement_state": "grug_managed", "degraded": True}


def test_get_enforcement_fallback_unknown_when_no_stored(_mod, monkeypatch):
    """Rate-limited AND no stored state → 'unknown' (NOT a false 'none')."""
    import httpx
    inst = _mod
    monkeypatch.setattr(inst, "get_installation", lambda i: {"installed_by_user_id": "999"})
    monkeypatch.setattr(inst, "with_install_token_retry",
                        lambda iid, fn: (_ for _ in ()).throw(httpx.ConnectError("dns")))
    monkeypatch.setattr(inst, "get_repo_config", lambda i, r: {})

    out = inst.get_enforcement(1, 2, user=_user(role="admin"))
    assert out == {"repo_id": 2, "enforcement_state": "unknown", "degraded": True}


def test_get_enforcement_404_propagates_not_swallowed(_mod, monkeypatch):
    """A legitimate 404 (repo not found) must NOT be caught by the httpx
    fallback — only rate-limit/transport errors degrade."""
    from fastapi import HTTPException
    inst = _mod
    monkeypatch.setattr(inst, "get_installation", lambda i: {"installed_by_user_id": "999"})

    def _raise_404(iid, fn):
        raise HTTPException(status_code=404, detail="repo not found")

    monkeypatch.setattr(inst, "with_install_token_retry", _raise_404)
    with pytest.raises(HTTPException) as exc:
        inst.get_enforcement(1, 2, user=_user(role="admin"))
    assert exc.value.status_code == 404
