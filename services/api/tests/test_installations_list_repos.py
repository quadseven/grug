"""Coverage for installations.list_install_repos GET endpoint.

PR #96/#107 covered list_installations + update_repo_config + the
_ensure_can_access helper, but the GET-repos endpoint that hits GH +
merges DDB per-repo config was uncovered. Closes pr-test-analyzer
HIGH gap #1.
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


def test_list_install_repos_unknown_install_404():
    with patch("installations.get_installation", return_value=None):
        with pytest.raises(HTTPException) as exc:
            inst.list_install_repos(install_id=999, user=_user())
    assert exc.value.status_code == 404


def test_list_install_repos_stranger_403():
    install = {"installed_by_user_id": "999"}
    with patch("installations.get_installation", return_value=install):
        with pytest.raises(HTTPException) as exc:
            inst.list_install_repos(install_id=1, user=_user(user_id="100"))
    assert exc.value.status_code == 403


def test_list_install_repos_merges_ddb_config_per_row():
    """Critical: the per-repo config (tpm_enabled toggle) must be
    merged into each repo row. SPA renders the toggle from this."""
    install = {"installed_by_user_id": "100"}

    def _retry(install_id, fn):
        return fn("tok")

    repos_resp = _ok_resp({"repositories": [
        {"id": 1, "full_name": "myorg/r1", "private": False, "default_branch": "main"},
        {"id": 2, "full_name": "myorg/r2", "private": True, "default_branch": "develop"},
    ]})

    def _get_cfg(install_id, repo_id):
        return {"tpm_enabled": False} if repo_id == 1 else {"tpm_enabled": True}

    with patch("installations.get_installation", return_value=install):
        with patch("installations.with_install_token_retry", side_effect=_retry):
            with patch("installations.get_repo_config", side_effect=_get_cfg):
                with patch("httpx.Client") as client_cls:
                    client = client_cls.return_value.__enter__.return_value
                    client.get.return_value = repos_resp
                    out = inst.list_install_repos(
                        install_id=1, user=_user(user_id="100"),
                    )

    assert len(out["repos"]) == 2
    by_id = {r["repo_id"]: r for r in out["repos"]}
    assert by_id[1]["config"] == {"tpm_enabled": False}
    assert by_id[2]["config"] == {"tpm_enabled": True}
    assert by_id[2]["private"] is True
    assert by_id[2]["default_branch"] == "develop"


def test_list_install_repos_malformed_payload_502():
    """silent-failure-hunter P1 #3 regression: missing 'repositories' key
    on the GET-repos endpoint must 502, not return empty list."""
    install = {"installed_by_user_id": "100"}

    def _retry(install_id, fn):
        return fn("tok")

    bad_resp = _ok_resp({"unexpected": "shape"})

    with patch("installations.get_installation", return_value=install):
        with patch("installations.with_install_token_retry", side_effect=_retry):
            with patch("httpx.Client") as client_cls:
                client = client_cls.return_value.__enter__.return_value
                client.get.return_value = bad_resp
                with pytest.raises(HTTPException) as exc:
                    inst.list_install_repos(
                        install_id=1, user=_user(user_id="100"),
                    )

    assert exc.value.status_code == 502


def test_list_install_repos_pagination_cap_at_10_pages():
    """Truncate at 1000 repos (10 × 100) + log warning. Larger orgs
    silently lose visibility past page 10. Not a v1 concern but the
    branch should not crash."""
    install = {"installed_by_user_id": "100"}

    full_page = _ok_resp({"repositories": [
        {"id": i, "full_name": f"myorg/r{i}", "private": False, "default_branch": "main"}
        for i in range(100)
    ]})

    def _retry(install_id, fn):
        return fn("tok")

    with patch("installations.get_installation", return_value=install):
        with patch("installations.with_install_token_retry", side_effect=_retry):
            with patch("installations.get_repo_config", return_value={"tpm_enabled": True}):
                with patch("httpx.Client") as client_cls:
                    client = client_cls.return_value.__enter__.return_value
                    # Always return a full page → loop hits page>10 cap
                    client.get.return_value = full_page
                    out = inst.list_install_repos(
                        install_id=1, user=_user(user_id="100"),
                    )

    # 10 pages × 100 repos = 1000 (cap). Loop breaks on page 11.
    assert len(out["repos"]) == 1000
