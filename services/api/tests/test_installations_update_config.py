"""Coverage for installations.update_repo_config — auth + GH membership.

The PUT /installations/{id}/repos/{id}/config endpoint enforces:
1. Install must exist (404 otherwise)
2. Caller must own install OR be admin (403 otherwise — see test_installations_routes)
3. Repo must belong to install (verified via GH /installation/repositories,
   not /repositories/{id} — Sentry CRITICAL on PR #43 closed that gap)
4. set_repo_config persists toggle

Tests mock get_installation + with_install_token_retry + httpx + set_repo_config
to avoid GH API + DDB round-trips. Auth path is shared with list_install_repos.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from fastapi import HTTPException

import installations as inst
from adapters.user_store import UserIdentity


def _user(user_id="100", role="user"):
    return UserIdentity(
        github_user_id=user_id, login="evan", role=role, tier="free",
        allowlisted=True, created_at="",
        allowlisted_at=None, allowlisted_by=None,
    )


def _ok_resp(json_body):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=json_body)
    return r


def test_update_repo_config_unknown_install_404():
    payload = inst.RepoConfigPayload(tpm_enabled=False)
    with patch("installations.get_installation", return_value=None):
        with pytest.raises(HTTPException) as exc:
            inst.update_repo_config(
                install_id=999, repo_id=1,
                body=payload, user=_user(),
            )
    assert exc.value.status_code == 404
    assert "install not found" in exc.value.detail


def test_update_repo_config_stranger_403():
    payload = inst.RepoConfigPayload(tpm_enabled=False)
    install = {"installed_by_user_id": "999"}
    with patch("installations.get_installation", return_value=install):
        with pytest.raises(HTTPException) as exc:
            inst.update_repo_config(
                install_id=1, repo_id=1,
                body=payload, user=_user(user_id="100"),
            )
    assert exc.value.status_code == 403


def test_update_repo_config_admin_can_access_any():
    payload = inst.RepoConfigPayload(tpm_enabled=False)
    install = {"installed_by_user_id": "999"}

    def _retry(install_id, fn):
        return fn("tok")

    fake_pages = [
        _ok_resp({"repositories": [{"id": 42, "full_name": "myorg/myrepo"}]}),
    ]

    with patch("installations.get_installation", return_value=install):
        with patch("installations.with_install_token_retry", side_effect=_retry):
            with patch("httpx.Client") as client_cls:
                client = client_cls.return_value.__enter__.return_value
                client.get.return_value = fake_pages[0]
                with patch("installations.get_repo_config", return_value={}), \
                     patch("installations.set_repo_config", return_value={"tpm_enabled": False}):
                    out = inst.update_repo_config(
                        install_id=1, repo_id=42,
                        body=payload, user=_user(user_id="100", role="admin"),
                    )

    assert out["repo_id"] == 42
    assert out["full_name"] == "myorg/myrepo"
    assert out["config"]["tpm_enabled"] is False


def test_update_repo_config_repo_not_in_install_404():
    """Sentry CRITICAL fix: repo not visible to install must 404 even if
    repo exists publicly elsewhere on GH."""
    payload = inst.RepoConfigPayload(tpm_enabled=True)
    install = {"installed_by_user_id": "100"}

    def _retry(install_id, fn):
        return fn("tok")

    fake_resp = _ok_resp({"repositories": [
        {"id": 1, "full_name": "myorg/repo1"},
        {"id": 2, "full_name": "myorg/repo2"},
    ]})

    with patch("installations.get_installation", return_value=install):
        with patch("installations.with_install_token_retry", side_effect=_retry):
            with patch("httpx.Client") as client_cls:
                client = client_cls.return_value.__enter__.return_value
                client.get.return_value = fake_resp
                with pytest.raises(HTTPException) as exc:
                    inst.update_repo_config(
                        install_id=1, repo_id=999,  # not in list
                        body=payload, user=_user(user_id="100"),
                    )
    assert exc.value.status_code == 404
    assert "not visible to install" in exc.value.detail


def test_update_repo_config_malformed_gh_502():
    """silent-failure-hunter P1 #3: missing 'repositories' key →
    502 not silent empty list."""
    payload = inst.RepoConfigPayload(tpm_enabled=True)
    install = {"installed_by_user_id": "100"}

    def _retry(install_id, fn):
        return fn("tok")

    fake_resp = _ok_resp({"unexpected": "shape"})

    with patch("installations.get_installation", return_value=install):
        with patch("installations.with_install_token_retry", side_effect=_retry):
            with patch("httpx.Client") as client_cls:
                client = client_cls.return_value.__enter__.return_value
                client.get.return_value = fake_resp
                with pytest.raises(HTTPException) as exc:
                    inst.update_repo_config(
                        install_id=1, repo_id=1,
                        body=payload, user=_user(user_id="100"),
                    )
    assert exc.value.status_code == 502
    assert "gh_upstream_malformed" in exc.value.detail


def test_update_repo_config_paginates_until_match():
    """Single-repo membership lookup must scan all pages — no early exit
    on full page when target repo lives on page 2+."""
    payload = inst.RepoConfigPayload(tpm_enabled=False)
    install = {"installed_by_user_id": "100"}

    page1 = _ok_resp({"repositories": [
        {"id": i, "full_name": f"myorg/r{i}"} for i in range(1, 101)
    ]})
    page2 = _ok_resp({"repositories": [
        {"id": 200, "full_name": "myorg/target"},
    ]})

    pages = [page1, page2]
    call_idx = [0]

    def _client_get(*args, **kwargs):
        resp = pages[call_idx[0]]
        call_idx[0] += 1
        return resp

    def _retry(install_id, fn):
        return fn("tok")

    with patch("installations.get_installation", return_value=install):
        with patch("installations.with_install_token_retry", side_effect=_retry):
            with patch("httpx.Client") as client_cls:
                client = client_cls.return_value.__enter__.return_value
                client.get.side_effect = _client_get
                with patch("installations.get_repo_config", return_value={}), \
                     patch("installations.set_repo_config", return_value={"tpm_enabled": False}):
                    out = inst.update_repo_config(
                        install_id=1, repo_id=200,
                        body=payload, user=_user(user_id="100"),
                    )

    assert out["full_name"] == "myorg/target"
    assert call_idx[0] == 2  # paginated past page 1
