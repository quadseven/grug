# MIRRORED — sibling at services/api/adapters/install_store.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
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

# Default for v1: TPM + Elder enabled on every repo unless an override
# row says otherwise. Newly-installed users get value out-of-the-box;
# opt-out is explicit per repo. `code_reviewer_blocking` defaults False
# (advisory mode: check-run conclusion=neutral, review event=COMMENT)
# so a noisy false-positive LLM run doesn't block PR velocity. Operator
# flips to blocking via dashboard once trust is established.
_DEFAULT_PERSONA_CONFIG = {
    "tpm_enabled": True,
    "code_reviewer_enabled": True,
    "code_reviewer_blocking": False,
}


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
        return {
            **_DEFAULT_PERSONA_CONFIG,
            "enforcement_ruleset_id": None,
            "force_disable_enforcement": False,
        }
    rid = item.get("enforcement_ruleset_id")
    return {
        "tpm_enabled": bool(item.get(
            "tpm_enabled", _DEFAULT_PERSONA_CONFIG["tpm_enabled"]
        )),
        "code_reviewer_enabled": bool(item.get(
            "code_reviewer_enabled",
            _DEFAULT_PERSONA_CONFIG["code_reviewer_enabled"],
        )),
        "code_reviewer_blocking": bool(item.get(
            "code_reviewer_blocking",
            _DEFAULT_PERSONA_CONFIG["code_reviewer_blocking"],
        )),
        "enforcement_ruleset_id": int(rid) if rid is not None else None,
        "force_disable_enforcement": bool(item.get("force_disable_enforcement", False)),
    }


def set_repo_config(
    *,
    install_id: int,
    repo_id: int,
    repo_full_name: str,
    tpm_enabled: bool,
    updated_by_user_id: str,
    code_reviewer_enabled: bool | None = None,
    code_reviewer_blocking: bool | None = None,
) -> dict[str, Any]:
    """Upsert per-repo override. Returns the FIELDS THAT WERE UPDATED
    (not the full resolved RepoConfig — callers needing that should
    follow up with `get_repo_config`).

    Uses update_item (not put_item) to preserve fields managed by
    other writers — e.g. enforcement_ruleset_id set by enforcement.py.

    `code_reviewer_*` kwargs are Optional so callers that only toggle
    TPM (e.g. legacy dashboard endpoints) don't have to pass them —
    None means "don't update this field." Falls back to the value
    already stored on the row, or to _DEFAULT_PERSONA_CONFIG.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Build SET expression dynamically — the kwargs are sparse.
    set_clauses = [
        "repo_full_name = :fn",
        "tpm_enabled = :te",
        "updated_at = :ua",
        "updated_by_user_id = :ub",
    ]
    values: dict[str, Any] = {
        ":fn": repo_full_name,
        ":te": bool(tpm_enabled),
        ":ua": now,
        ":ub": str(updated_by_user_id),
    }
    if code_reviewer_enabled is not None:
        set_clauses.append("code_reviewer_enabled = :cre")
        values[":cre"] = bool(code_reviewer_enabled)
    if code_reviewer_blocking is not None:
        set_clauses.append("code_reviewer_blocking = :crb")
        values[":crb"] = bool(code_reviewer_blocking)

    _table.update_item(
        Key={"PK": _inst_pk(install_id), "SK": _repo_sk(repo_id)},
        UpdateExpression="SET " + ", ".join(set_clauses),
        ExpressionAttributeValues=values,
    )
    updated_fields = {"tpm_enabled": bool(tpm_enabled)}
    if code_reviewer_enabled is not None:
        updated_fields["code_reviewer_enabled"] = bool(code_reviewer_enabled)
    if code_reviewer_blocking is not None:
        updated_fields["code_reviewer_blocking"] = bool(code_reviewer_blocking)
    return updated_fields


def get_enforcement_id(install_id: int, repo_id: int) -> int | None:
    """Return the stored Grug-managed ruleset ID, or None."""
    resp = _table.get_item(
        Key={"PK": _inst_pk(install_id), "SK": _repo_sk(repo_id)},
    )
    item = resp.get("Item")
    if not item:
        return None
    val = item.get("enforcement_ruleset_id")
    return int(val) if val is not None else None


def set_enforcement_id(
    install_id: int, repo_id: int, ruleset_id: int | None,
) -> None:
    """Update only the enforcement_ruleset_id field on a RepoConfig row."""
    if ruleset_id is not None:
        _table.update_item(
            Key={"PK": _inst_pk(install_id), "SK": _repo_sk(repo_id)},
            UpdateExpression="SET enforcement_ruleset_id = :rid",
            ExpressionAttributeValues={":rid": ruleset_id},
        )
    else:
        _table.update_item(
            Key={"PK": _inst_pk(install_id), "SK": _repo_sk(repo_id)},
            UpdateExpression="REMOVE enforcement_ruleset_id",
        )


def is_persona_enabled(install_id: int, repo_id: int, persona: str) -> bool:
    """Webhook-style check: is `persona` enabled for this repo?

    Mirrored into services/webhook/adapters/install_store.py — the
    webhook calls this before TPM dispatch so a user can disable Grug
    on a noisy repo without uninstalling.
    """
    cfg = get_repo_config(install_id, repo_id)
    key = f"{persona}_enabled"
    return bool(cfg.get(key, _DEFAULT_PERSONA_CONFIG.get(key, True)))
