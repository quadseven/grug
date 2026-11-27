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
        github_user_id=user_id, login="alice", role=role, tier="free",
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
    merged into each repo row in a single batch query. Unconfigured repos
    receive the normalized default config."""
    install = {"installed_by_user_id": "100"}

    def _retry(install_id, fn):
        return fn("tok")

    repos_resp = _ok_resp({"repositories": [
        {"id": 1, "full_name": "myorg/r1", "private": False, "default_branch": "main"},
        {"id": 2, "full_name": "myorg/r2", "private": True, "default_branch": "develop"},
        {"id": 3, "full_name": "myorg/r3", "private": False, "default_branch": "main"},
    ]})

    batch_cfgs = {
        1: {"tpm_enabled": False},
        2: {"tpm_enabled": True},
    }

    with patch("installations.get_installation", return_value=install):
        with patch("installations.with_install_token_retry", side_effect=_retry):
            with patch("installations.get_repo_configs", return_value=batch_cfgs) as mock_batch:
                with patch("installations.get_repo_config") as mock_single:
                    with patch("httpx.Client") as client_cls:
                        client = client_cls.return_value.__enter__.return_value
                        client.get.return_value = repos_resp
                        out = inst.list_install_repos(
                            install_id=1, user=_user(user_id="100"),
                        )

    mock_batch.assert_called_once_with(1)
    mock_single.assert_not_called()
    assert len(out["repos"]) == 3
    by_id = {r["repo_id"]: r for r in out["repos"]}
    assert by_id[1]["config"] == {"tpm_enabled": False}
    assert by_id[2]["config"] == {"tpm_enabled": True}
    assert by_id[2]["private"] is True
    assert by_id[2]["default_branch"] == "develop"
    assert by_id[3]["config"] == inst.default_repo_config()


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
    """Truncate at 1000 repos (10 x 100) + log warning. Larger orgs
    silently lose visibility past page 10. Batch config fetch runs once."""
    install = {"installed_by_user_id": "100"}

    full_page = _ok_resp({"repositories": [
        {"id": i, "full_name": f"myorg/r{i}", "private": False, "default_branch": "main"}
        for i in range(100)
    ]})

    def _retry(install_id, fn):
        return fn("tok")

    with patch("installations.get_installation", return_value=install):
        with patch("installations.with_install_token_retry", side_effect=_retry):
            with patch("installations.get_repo_configs", return_value={}) as mock_batch:
                with patch("installations.get_repo_config") as mock_single:
                    with patch("httpx.Client") as client_cls:
                        client = client_cls.return_value.__enter__.return_value
                        # Always return a full page -> loop hits page>10 cap
                        client.get.return_value = full_page
                        out = inst.list_install_repos(
                            install_id=1, user=_user(user_id="100"),
                        )

    # 10 pages x 100 repos = 1000 (cap). Loop breaks on page 11.
    assert len(out["repos"]) == 1000
    mock_batch.assert_called_once_with(1)
    mock_single.assert_not_called()


def test_get_repo_configs_batches_and_skips_unparsable_sk():
    """Unit test for pg_install_store.get_repo_configs without Postgres:
    runs one SELECT with REPO#%% + TTL_LIVE, decodes valid rows identically
    to get_repo_config, and skips unparsable sort keys."""
    from adapters import pg_install_store as store
    from adapters.pg_base import TTL_LIVE

    fake_rows = [
        (store._repo_sk(10), {"tpm_enabled": False, "enforcement_ruleset_id": 77, "elder_voice": "sage"}),
        (store._repo_sk(20), {"dep_watch_enabled": True, "complexity_cyclomatic_cap": 12}),
        (store._repo_sk("not-an-int"), {"tpm_enabled": False}),
    ]
    executed: list[tuple[str, tuple]] = []

    class _FakeCursor:
        def fetchall(self):
            return fake_rows

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            executed.append((sql, params))
            return _FakeCursor()

    class _FakePool:
        def connection(self):
            return _FakeConn()

    with patch.object(store, "get_pool", return_value=_FakePool()):
        result = store.get_repo_configs(42)

    assert len(executed) == 1
    sql, params = executed[0]
    assert "sk LIKE 'REPO#%%'" in sql
    assert TTL_LIVE in sql
    assert params == (store._inst_pk(42),)
    assert set(result.keys()) == {10, 20}
    assert result[10]["tpm_enabled"] is False
    assert result[10]["enforcement_ruleset_id"] == 77
    assert result[10]["elder_voice"] == "sage"
    assert result[10]["code_reviewer_enabled"] is True
    assert result[20]["dep_watch_enabled"] is True
    assert result[20]["complexity_cyclomatic_cap"] == 12
    assert result[20]["tpm_enabled"] is True
