"""User-facing installation + per-repo config endpoints (Slice 7 #28).

3 endpoints, all session-cookie-authed (allowlist NOT required — users
need to see their own installs even before admin allowlists them so
they know to wait):

  GET  /api/v1/installations
       → INST# rows installed by the current user

  GET  /api/v1/installations/{install_id}/repos
       → repos visible to that install (calls GitHub via install token,
         then merges per-repo config from DDB)

  PUT  /api/v1/installations/{install_id}/repos/{repo_id}/config
       → upsert per-repo persona override (e.g. {"tpm_enabled": false})

Authorization model: caller must own the install (sender.id ==
INST#installed_by_user_id). Admins bypass the ownership check.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from adapters.install_store import (
    get_installation,
    get_repo_config,
    list_user_installations,
    set_repo_config,
)
from adapters.user_store import User
from auth.dependencies import require_authenticated
from github_app_auth import get_install_token

log = logging.getLogger("grug.api.installations")

router = APIRouter(prefix="/api/v1")

_GH_API = "https://api.github.com"


class RepoConfigPayload(BaseModel):
    tpm_enabled: bool = Field(default=True)


def _ensure_can_access(install: dict[str, Any], user: User) -> None:
    """Caller must own the install OR be admin. Raises 403 otherwise."""
    if user.role == "admin":
        return
    owner_id = install.get("installed_by_user_id", "")
    if str(owner_id) != str(user.github_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not your installation",
        )


@router.get("/installations")
def list_installations(user: User = Depends(require_authenticated)) -> dict[str, Any]:
    """Installs owned by the current user (admin sees only own here too;
    cross-user listing is admin-only and lives at /api/v1/admin/users)."""
    items = list_user_installations(user.github_user_id)
    return {
        "installations": [
            {
                "install_id": int(it["PK"].split("#", 1)[1]),
                "account_login": it.get("account_login", ""),
                "account_type": it.get("account_type", "User"),
                "installed_at": it.get("installed_at", ""),
            }
            for it in items
        ],
    }


@router.get("/installations/{install_id}/repos")
def list_install_repos(
    install_id: int,
    user: User = Depends(require_authenticated),
) -> dict[str, Any]:
    """List repos visible to this install (live from GitHub) merged with
    DDB per-repo config so the SPA can render toggle state."""
    install = get_installation(install_id)
    if not install:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="install not found")
    _ensure_can_access(install, user)

    token = get_install_token(install_id)
    repos: list[dict[str, Any]] = []
    page = 1
    with httpx.Client(timeout=10) as client:
        while True:
            resp = client.get(
                f"{_GH_API}/installation/repositories",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            body = resp.json()
            for r in body.get("repositories", []):
                cfg = get_repo_config(install_id, r["id"])
                repos.append({
                    "repo_id": r["id"],
                    "full_name": r["full_name"],
                    "private": r.get("private", False),
                    "default_branch": r.get("default_branch", "main"),
                    "config": cfg,
                })
            if len(body.get("repositories", [])) < 100:
                break
            page += 1
            if page > 10:  # 1000 repos is plenty for v1
                log.warning(
                    "list_repos_pagination_cap",
                    extra={"install_id": install_id, "user": user.login},
                )
                break

    return {"repos": repos}


@router.put("/installations/{install_id}/repos/{repo_id}/config")
def update_repo_config(
    install_id: int,
    repo_id: int,
    body: RepoConfigPayload,
    user: User = Depends(require_authenticated),
) -> dict[str, Any]:
    """Upsert per-repo persona toggle. Caller must own the install."""
    install = get_installation(install_id)
    if not install:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="install not found")
    _ensure_can_access(install, user)

    # Verify repo really belongs to the install — stops a caller from
    # setting overrides for repos they can't reach.
    #
    # Sentry CRITICAL on PR #43: earlier check used `GET /repositories/{id}`
    # which returns 200 for ANY public repo regardless of install access.
    # Now enumerate via `GET /installation/repositories` (the dedicated
    # endpoint that lists ONLY this install's repos) and verify
    # membership.
    token = get_install_token(install_id)
    full_name = ""
    found = False
    page = 1
    with httpx.Client(timeout=10) as client:
        while not found:
            resp = client.get(
                f"{_GH_API}/installation/repositories",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            body_json = resp.json()
            for r in body_json.get("repositories", []):
                if r["id"] == repo_id:
                    found = True
                    full_name = r["full_name"]
                    break
            if found or len(body_json.get("repositories", [])) < 100:
                break
            page += 1
            # No page cap — single-repo membership lookup must scan all
            # pages on large org installs (>1000 repos). Codex P2
            # follow-up to the Sentry CRITICAL fix. Worst case ~Npages*
            # 100ms; acceptable for an admin write path.
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="repo not visible to install")

    cfg = set_repo_config(
        install_id=install_id, repo_id=repo_id,
        repo_full_name=full_name, tpm_enabled=body.tpm_enabled,
        updated_by_user_id=user.github_user_id,
    )
    log.info(
        "repo_config_updated",
        extra={
            "install_id": install_id, "repo_id": repo_id,
            "full_name": full_name, "by_user": user.login,
            **cfg,
        },
    )
    return {"repo_id": repo_id, "full_name": full_name, "config": cfg}
