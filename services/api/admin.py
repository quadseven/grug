"""Admin-only user + allowlist management (Slice 8 #29).

Endpoints — all gated by `require_admin`:
  GET  /api/v1/admin/users              → all USER# rows
  PATCH /api/v1/admin/users/{user_id}   → flip allowlisted / role / tier
  GET  /api/v1/admin/installations      → all INST# rows (cross-user)

Full prefix-fetch instead of a filtered/paged query — admin endpoint,
low traffic, < 100 rows in v1 (Evan + GF + handful of beta testers).
At real scale we'd add a dedicated index/predicate; for v1 that's
premature optimization.

Audit trail: every PATCH logs to DD with structured `admin_user_patched`
event including actor + before/after diff. No audit table in the store per
locked PRD ("DD unlimited tier handles audit").
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from adapters.user_store import UserIdentity, get_user_item, scan_meta_items, update_user_fields
from auth.dependencies import require_admin

log = logging.getLogger("grug.api.admin")

router = APIRouter(prefix="/api/v1/admin")


def _user_to_admin_view(item: dict[str, Any]) -> dict[str, Any]:
    """Project a USER# store row into the admin response shape.

    Excludes oauth_*_blob ciphertext — admin doesn't need plaintext
    tokens AND including blobs in JSON responses would let an admin
    accidentally exfil them in logs/screenshots.
    """
    return {
        "github_user_id": item["PK"].split("#", 1)[1],
        "login": item.get("login", ""),
        "role": item.get("role", "user"),
        "tier": item.get("tier", "free"),
        "allowlisted": bool(item.get("allowlisted", False)),
        "created_at": item.get("created_at", ""),
        "last_login_at": item.get("last_login_at", ""),
        "allowlisted_at": item.get("allowlisted_at"),
        "allowlisted_by": item.get("allowlisted_by"),
    }


def _inst_to_admin_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "install_id": int(item["PK"].split("#", 1)[1]),
        "account_login": item.get("account_login", ""),
        "account_type": item.get("account_type", "User"),
        "installed_at": item.get("installed_at", ""),
        "installed_by_user_id": item.get("installed_by_user_id", ""),
    }


def _scan_all(*, pk_prefix: str) -> list[dict[str, Any]]:
    """Live META rows by PK prefix - a named store operation since the
    #354 swap (the DDB Scan pagination this wrapped lives in history)."""
    return scan_meta_items(pk_prefix=pk_prefix)


@router.get("/users")
def list_users(_: UserIdentity = Depends(require_admin)) -> dict[str, Any]:
    """All USER# rows."""
    return {"users": [_user_to_admin_view(it) for it in _scan_all(pk_prefix="USER#")]}


@router.get("/installations")
def list_all_installations(_: UserIdentity = Depends(require_admin)) -> dict[str, Any]:
    """All INST# rows across all users."""
    return {
        "installations": [_inst_to_admin_view(it) for it in _scan_all(pk_prefix="INST#")],
    }


class UserPatchPayload(BaseModel):
    allowlisted: bool | None = None
    role: str | None = None  # "admin" | "user"
    tier: str | None = None  # "lifetime" | "free" | "paid"


_VALID_ROLES = {"admin", "user"}
_VALID_TIERS = {"lifetime", "free", "paid"}


@router.patch("/users/{user_id}")
def patch_user(
    user_id: str,
    payload: UserPatchPayload,
    actor: UserIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """Flip allowlisted / role / tier on a user. Audit log to DD."""
    if payload.role is not None and payload.role not in _VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"role must be one of {sorted(_VALID_ROLES)}",
        )
    if payload.tier is not None and payload.tier not in _VALID_TIERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tier must be one of {sorted(_VALID_TIERS)}",
        )

    # Self-protection: admin can't demote themselves to user role.
    # (Prevents the only-admin lock-out scenario.)
    if (
        payload.role is not None
        and payload.role != "admin"
        and str(user_id) == str(actor.github_user_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot demote yourself; ask another admin",
        )

    existing = get_user_item(str(user_id))
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    before = {
        "allowlisted": bool(existing.get("allowlisted", False)),
        "role": existing.get("role", "user"),
        "tier": existing.get("tier", "free"),
    }
    after = dict(before)
    fields: dict[str, Any] = {}
    now = datetime.now(timezone.utc).isoformat()

    if payload.allowlisted is not None:
        fields["allowlisted"] = payload.allowlisted
        after["allowlisted"] = payload.allowlisted
        if payload.allowlisted and not before["allowlisted"]:
            fields["allowlisted_at"] = now
            # Record the immutable github_user_id, not the (mutable) login.
            fields["allowlisted_by"] = actor.github_user_id
    if payload.role is not None:
        fields["role"] = payload.role
        after["role"] = payload.role
    if payload.tier is not None:
        fields["tier"] = payload.tier
        after["tier"] = payload.tier

    if not fields:
        return {"user_id": user_id, "before": before, "after": after, "changed": False}

    try:
        new = update_user_fields(str(user_id), fields)
    except LookupError:
        # Row existed at the read above but vanished mid-update (raced a
        # deletion). 404, not a 500 — the client retries and sees reality.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="user not found"
        ) from None

    log.info(
        "admin_user_patched",
        extra={
            "actor_login": actor.login,
            "actor_id": actor.github_user_id,
            "target_user_id": user_id,
            "before": before,
            "after": after,
        },
    )
    return {
        "user_id": user_id,
        "before": before,
        "after": after,
        "changed": True,
        "user": _user_to_admin_view(new),
    }
