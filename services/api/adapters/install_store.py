# MIRRORED — sibling at services/webhook/adapters/install_store.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
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

# Lazy init — boto3 resource construction at import time would require
# AWS_DEFAULT_REGION to be set BEFORE the first import, which breaks
# tests that monkeypatch the env after collection. Codex post-review
# #52. The descriptor on `_table` lets old call sites (`_table.scan`,
# `_table.get_item`, etc.) keep working unchanged.
#
# Thread-safety via double-checked locking: a warm Lambda handling two
# concurrent invocations could race the unguarded check; both would call
# boto3.resource() and one of the two resource handles would leak. The
# lock + re-check after acquire is the canonical fix and adds zero
# LOCK cost after the first call — the outer `is None` short-circuits
# without acquiring the lock; per-attribute `__getattr__` indirection
# remains. Peer-review HIGH (openrouter).
import threading

_ddb = None
_table_real = None
_init_lock = threading.Lock()


class _LazyTable:
    def __getattr__(self, name):
        global _ddb, _table_real
        if _table_real is None:
            with _init_lock:
                if _table_real is None:
                    _ddb = boto3.resource("dynamodb")
                    _table_real = _ddb.Table(_TABLE_NAME)
        return getattr(_table_real, name)


_table = _LazyTable()


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

    Uses an atomic UpdateExpression with `if_not_exists(installed_at)`
    so concurrent duplicate webhook deliveries can't race-overwrite
    the original install timestamp (Codex P2 follow-up to Greptile P2
    on PR #41 — read-then-put had a race window between two concurrent
    Lambda invocations).
    """
    now = datetime.now(timezone.utc).isoformat()
    _table.update_item(
        Key={"PK": _inst_pk(install_id), "SK": "META"},
        UpdateExpression=(
            "SET account_login = :login, "
            "account_type = :atype, "
            "installed_by_user_id = :by, "
            "GSI1PK = :gsi1pk, "
            "GSI1SK = :gsi1sk, "
            # if_not_exists preserves the original timestamp when the
            # row already has one — concurrent-safe.
            "installed_at = if_not_exists(installed_at, :now)"
        ),
        ExpressionAttributeValues={
            ":login": account_login,
            ":atype": account_type,
            ":by": str(installed_by_user_id),
            ":gsi1pk": str(installed_by_user_id),
            ":gsi1sk": _inst_pk(install_id),
            ":now": now,
        },
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


# ---------------------------------------------------------------------------
# Slice 7 (#28) — per-repo persona toggles
# ---------------------------------------------------------------------------

# Default for v1: TPM enabled on every repo unless an override row says
# otherwise. (Newly-installed users get value out-of-the-box; opt-out is
# explicit per repo.)
_DEFAULT_PERSONA_CONFIG = {"tpm_enabled": True}


def _repo_sk(repo_id: int | str) -> str:
    return f"REPO#{repo_id}"


def list_user_installations(github_user_id: str) -> list[dict[str, Any]]:
    """Return INST# rows installed by this user via the GSI1 index."""
    resp = _table.query(
        IndexName="GSI1",
        KeyConditionExpression="GSI1PK = :pk AND begins_with(GSI1SK, :sk)",
        ExpressionAttributeValues={
            ":pk": str(github_user_id),
            ":sk": "INST#",
        },
    )
    return resp.get("Items", [])


def get_repo_config(install_id: int, repo_id: int) -> dict[str, Any]:
    """Per-repo persona override; returns defaults if no row exists."""
    resp = _table.get_item(
        Key={"PK": _inst_pk(install_id), "SK": _repo_sk(repo_id)},
    )
    item = resp.get("Item")
    if not item:
        return dict(_DEFAULT_PERSONA_CONFIG)
    return {
        "tpm_enabled": bool(item.get("tpm_enabled", _DEFAULT_PERSONA_CONFIG["tpm_enabled"])),
    }


def set_repo_config(
    *,
    install_id: int,
    repo_id: int,
    repo_full_name: str,
    tpm_enabled: bool,
    updated_by_user_id: str,
) -> dict[str, Any]:
    """Upsert per-repo override. Returns the resolved config."""
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "PK": _inst_pk(install_id),
        "SK": _repo_sk(repo_id),
        "repo_full_name": repo_full_name,
        "tpm_enabled": bool(tpm_enabled),
        "updated_at": now,
        "updated_by_user_id": str(updated_by_user_id),
    }
    _table.put_item(Item=item)
    return {"tpm_enabled": item["tpm_enabled"]}


def is_persona_enabled(install_id: int, repo_id: int, persona: str) -> bool:
    """Webhook-style check: is `persona` enabled for this repo?

    Mirrored into services/webhook/adapters/install_store.py — the
    webhook calls this before TPM dispatch so a user can disable Grug
    on a noisy repo without uninstalling.
    """
    cfg = get_repo_config(install_id, repo_id)
    key = f"{persona}_enabled"
    return bool(cfg.get(key, _DEFAULT_PERSONA_CONFIG.get(key, True)))
