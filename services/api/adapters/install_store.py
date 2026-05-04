"""Webhook-side install store + allowlist gate.

Webhook Lambda has DDB read+write perms but NO KMS perms — it never
touches OAuth tokens (those belong to api Lambda). This module exposes
ONLY the bool `allowlisted` field plus install-row CRUD.

Single-table layout:
  PK = INST#<install_id>     SK = META
    account_login, account_type, installed_at, installed_by_user_id
  PK = USER#<github_user_id> SK = META
    allowlisted (read here; oauth_*_blob untouched)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3

log = logging.getLogger("grug.webhook.install_store")

_TABLE_NAME = os.environ.get("GRUG_DDB_TABLE", "grug-main")
_ddb = boto3.resource("dynamodb")
_table = _ddb.Table(_TABLE_NAME)


def _inst_pk(install_id: int | str) -> str:
    return f"INST#{install_id}"


def _user_pk(github_user_id: int | str) -> str:
    return f"USER#{github_user_id}"


def record_installation(
    *,
    install_id: int,
    account_login: str,
    account_type: str,
    installed_by_user_id: int,
) -> None:
    """Idempotent upsert of INST#<id> META row on `installation:created`.

    On `installation:deleted` callers should use `delete_installation`
    instead — this function only writes.
    """
    now = datetime.now(timezone.utc).isoformat()
    _table.put_item(
        Item={
            "PK": _inst_pk(install_id),
            "SK": "META",
            "account_login": account_login,
            "account_type": account_type,
            "installed_at": now,
            "installed_by_user_id": str(installed_by_user_id),
            # GSI1 — list installations by user (per PRD #21 schema).
            "GSI1PK": str(installed_by_user_id),
            "GSI1SK": _inst_pk(install_id),
        }
    )


def delete_installation(install_id: int) -> None:
    _table.delete_item(Key={"PK": _inst_pk(install_id), "SK": "META"})


def get_installation(install_id: int) -> dict[str, Any] | None:
    resp = _table.get_item(
        Key={"PK": _inst_pk(install_id), "SK": "META"},
        ConsistentRead=True,
    )
    return resp.get("Item")


def is_install_allowlisted(install_id: int) -> bool:
    """Return True iff INST#<id> exists AND its installer is allowlisted.

    Two-hop lookup — INST# row holds installed_by_user_id; USER# row
    holds the actual `allowlisted` bool. Returns False on any miss
    (unknown install, unknown user, allowlisted=False/missing).

    Webhook-safe: reads `allowlisted` directly, never touches token
    blobs (no KMS Decrypt call).
    """
    inst = get_installation(install_id)
    if not inst:
        log.info("allowlist_miss_no_install", extra={"install_id": install_id})
        return False
    user_id = inst.get("installed_by_user_id")
    if not user_id:
        log.warning(
            "allowlist_install_missing_user_id",
            extra={"install_id": install_id},
        )
        return False
    user = _table.get_item(
        Key={"PK": _user_pk(user_id), "SK": "META"},
        ConsistentRead=True,
        ProjectionExpression="allowlisted",
    ).get("Item")
    if not user:
        log.info(
            "allowlist_miss_no_user",
            extra={"install_id": install_id, "user_id": user_id},
        )
        return False
    allowlisted = bool(user.get("allowlisted", False))
    if not allowlisted:
        log.info(
            "allowlist_miss_user_not_allowlisted",
            extra={"install_id": install_id, "user_id": user_id},
        )
    return allowlisted
