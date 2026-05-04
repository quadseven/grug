"""Admin-only user + allowlist management (Slice 8 #29).

Endpoints — all gated by `require_admin`:
  GET  /api/v1/admin/users              → all USER# rows (paginated)
  PATCH /api/v1/admin/users/{user_id}   → flip allowlisted / role / tier
  GET  /api/v1/admin/installations      → all INST# rows (cross-user)

DDB Scan instead of Query — admin endpoint, low traffic, < 100 rows in
v1 (Evan + GF + handful of beta testers). At ~100 users we'd switch to
a `BY=admin` GSI; for v1 that's premature optimization.

Audit trail: every PATCH logs to DD with structured `admin_user_patched`
event including actor + before/after diff. No DDB audit table per
locked PRD ("DD unlimited tier handles audit").
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from adapters.user_store import User, _table  # type: ignore[reportPrivateUsage]
from auth.dependencies import require_admin

log = logging.getLogger("grug.api.admin")

router = APIRouter(prefix="/api/v1/admin")


def _user_to_admin_view(item: dict[str, Any]) -> dict[str, Any]:
    """Project a USER# DDB row into the admin response shape.

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
    """Page through DDB Scan with PK-prefix + SK=META filter.

    DDB Scan returns at most 1 MB per page; LastEvaluatedKey signals
    more remain. Filter is applied AFTER the page read so 1 MB of
    REPO# rows can yield zero matching USER# items in a single page —
    must paginate. Capped at 50 pages defensively to bound admin
    endpoint runtime.
    """
    items: list[dict[str, Any]] = []
    last_key: dict[str, Any] | None = None
    pages = 0
    while True:
        kwargs = {
            "FilterExpression": "begins_with(PK, :prefix) AND SK = :sk",
            "ExpressionAttributeValues": {":prefix": pk_prefix, ":sk": "META"},
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        resp = _table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        pages += 1
        if last_key is None or pages >= 50:
            if last_key is not None:
                log.warning(
                    "admin_scan_truncated",
                    extra={"pk_prefix": pk_prefix, "pages_read": pages},
                )
            break
    return items


@router.get("/users")
def list_users(_: User = Depends(require_admin)) -> dict[str, Any]:
    """All USER# rows."""
    return {"users": [_user_to_admin_view(it) for it in _scan_all(pk_prefix="USER#")]}


@router.get("/installations")
def list_all_installations(_: User = Depends(require_admin)) -> dict[str, Any]:
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
    actor: User = Depends(require_admin),
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

    pk = f"USER#{user_id}"
    existing = _table.get_item(Key={"PK": pk, "SK": "META"}).get("Item")
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    before = {
        "allowlisted": bool(existing.get("allowlisted", False)),
        "role": existing.get("role", "user"),
        "tier": existing.get("tier", "free"),
    }
    after = dict(before)
    update_parts: list[str] = []
    expr_vals: dict[str, Any] = {}
    expr_names: dict[str, str] = {}
    now = datetime.now(timezone.utc).isoformat()

    if payload.allowlisted is not None:
        update_parts.append("allowlisted = :a")
        expr_vals[":a"] = payload.allowlisted
        after["allowlisted"] = payload.allowlisted
        if payload.allowlisted and not before["allowlisted"]:
            update_parts.append("allowlisted_at = :at, allowlisted_by = :by")
            expr_vals[":at"] = now
            expr_vals[":by"] = actor.login
    if payload.role is not None:
        # `role` is a DDB reserved word — must alias.
        update_parts.append("#r = :r")
        expr_names["#r"] = "role"
        expr_vals[":r"] = payload.role
        after["role"] = payload.role
    if payload.tier is not None:
        update_parts.append("tier = :t")
        expr_vals[":t"] = payload.tier
        after["tier"] = payload.tier

    if not update_parts:
        return {"user_id": user_id, "before": before, "after": after, "changed": False}

    kwargs: dict[str, Any] = {
        "Key": {"PK": pk, "SK": "META"},
        "UpdateExpression": "SET " + ", ".join(update_parts),
        "ExpressionAttributeValues": expr_vals,
        "ReturnValues": "ALL_NEW",
    }
    if expr_names:
        kwargs["ExpressionAttributeNames"] = expr_names

    new = _table.update_item(**kwargs).get("Attributes", {})

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
