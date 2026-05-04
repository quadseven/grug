"""DDB user store with KMS envelope encryption for OAuth tokens.

Single-table layout (per PRD #21):
  PK = USER#<github_user_id>
  SK = META

oauth_access_token + oauth_refresh_token are encrypted via the KMS
envelope (services/api/crypto/kms_envelope.py) BEFORE write. DDB sees
opaque ciphertext; CloudTrail logs every kms.Decrypt with the
EncryptionContext bound (anti-row-transplant defense).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("grug.api.adapters.user_store")

_TABLE_NAME = os.environ.get("GRUG_DDB_TABLE", "grug-main")

# Lazy init — see install_store.py for rationale (Codex post-review #52).
_ddb = None
_table_real = None


class _LazyTable:
    def __getattr__(self, name):
        global _ddb, _table_real
        if _table_real is None:
            _ddb = boto3.resource("dynamodb")
            _table_real = _ddb.Table(_TABLE_NAME)
        return getattr(_table_real, name)


_table = _LazyTable()


@dataclass
class User:
    github_user_id: str
    login: str
    role: str  # "admin" | "user"
    tier: str  # "lifetime" | "free" | "paid"
    allowlisted: bool
    oauth_access_token: str  # plaintext after decrypt
    oauth_refresh_token: str | None
    created_at: str
    allowlisted_at: str | None
    allowlisted_by: str | None


def _user_pk(github_user_id: str) -> str:
    return f"USER#{github_user_id}"


def get_user(github_user_id: str) -> User | None:
    """Return the user row (with decrypted OAuth tokens) or None.

    KMS Decrypt happens here (no plaintext caching per the envelope
    contract). Returns None when the user has never signed in.
    """
    from crypto.kms_envelope import decrypt_for_user  # lazy import

    try:
        resp = _table.get_item(
            Key={"PK": _user_pk(github_user_id), "SK": "META"},
            ConsistentRead=True,
        )
    except ClientError as e:
        # Distinguish DDB throttle/transient/IAM from a legitimate miss
        # (resp.get("Item") absent). Without this, a transient
        # ProvisionedThroughputExceededException would surface as a
        # generic 500 with no DDB context — and worse, get_current_user
        # would return None, spuriously logging the user out.
        # silent-failure-hunter P1 #2.
        log.error(
            "user_store_get_item_failed",
            extra={
                "github_user_id": github_user_id,
                "code": e.response.get("Error", {}).get("Code", "unknown"),
            },
        )
        raise
    item = resp.get("Item")
    if not item:
        return None

    encrypted_token = item.get("oauth_access_token_blob")
    if encrypted_token:
        # boto3 may return DDB Binary as a Binary wrapper (with .value)
        # or raw bytes depending on resource-vs-client mode — handle both.
        blob = encrypted_token.value if hasattr(encrypted_token, "value") else encrypted_token
        access_token = decrypt_for_user(
            blob=blob, user_id=github_user_id, item_type="oauth_access_token",
        )
    else:
        access_token = ""

    encrypted_refresh = item.get("oauth_refresh_token_blob")
    refresh_token: str | None = None
    if encrypted_refresh:
        blob = encrypted_refresh.value if hasattr(encrypted_refresh, "value") else encrypted_refresh
        refresh_token = decrypt_for_user(
            blob=blob, user_id=github_user_id, item_type="oauth_refresh_token",
        )

    return User(
        github_user_id=github_user_id,
        login=item.get("login", ""),
        role=item.get("role", "user"),
        tier=item.get("tier", "free"),
        allowlisted=bool(item.get("allowlisted", False)),
        oauth_access_token=access_token,
        oauth_refresh_token=refresh_token,
        created_at=item.get("created_at", ""),
        allowlisted_at=item.get("allowlisted_at"),
        allowlisted_by=item.get("allowlisted_by"),
    )


def upsert_oauth_user(
    *,
    github_user_id: str,
    login: str,
    oauth_access_token: str,
    oauth_refresh_token: str | None = None,
) -> User:
    """Create-or-update a user row from the OAuth callback flow.

    Defaults:
      role=user, tier=free, allowlisted=false  (gated until admin flips)

    Existing rows preserve their role/tier/allowlisted state — only the
    OAuth tokens + last_login_at update.
    """
    from crypto.kms_envelope import encrypt_for_user  # lazy import

    now = datetime.now(timezone.utc).isoformat()

    existing = _table.get_item(
        Key={"PK": _user_pk(github_user_id), "SK": "META"}
    ).get("Item")

    encrypted_access = encrypt_for_user(
        plaintext=oauth_access_token,
        user_id=github_user_id,
        item_type="oauth_access_token",
    )
    encrypted_refresh = None
    if oauth_refresh_token:
        encrypted_refresh = encrypt_for_user(
            plaintext=oauth_refresh_token,
            user_id=github_user_id,
            item_type="oauth_refresh_token",
        )

    item: dict[str, Any] = {
        "PK": _user_pk(github_user_id),
        "SK": "META",
        "login": login,
        "oauth_access_token_blob": encrypted_access,
        "last_login_at": now,
    }
    if encrypted_refresh:
        item["oauth_refresh_token_blob"] = encrypted_refresh
    elif existing and "oauth_refresh_token_blob" in existing:
        # Sentry HIGH on PR #39 — GitHub OAuth re-auth may return access
        # token without rotating refresh. put_item overwrites the whole
        # row, so without preserving the existing refresh blob we'd
        # silently nuke it and break refresh on next access expiry.
        item["oauth_refresh_token_blob"] = existing["oauth_refresh_token_blob"]

    if existing:
        # Preserve admin / tier / allowlist state
        for k in ("role", "tier", "allowlisted", "created_at",
                  "allowlisted_at", "allowlisted_by"):
            if k in existing:
                item[k] = existing[k]
    else:
        # New user defaults
        item["role"] = "user"
        item["tier"] = "free"
        item["allowlisted"] = False
        item["created_at"] = now

    _table.put_item(Item=item)

    # If we preserved an existing refresh blob (re-auth without rotation),
    # decrypt it so the returned User reflects what's now in the row.
    # Otherwise callers using the return value would think refresh is
    # gone even though the row still has it. Codex follow-up to the
    # PR #39 Sentry HIGH fix.
    returned_refresh = oauth_refresh_token
    if returned_refresh is None and existing and "oauth_refresh_token_blob" in existing:
        from crypto.kms_envelope import decrypt_for_user
        blob = existing["oauth_refresh_token_blob"]
        blob_bytes = blob.value if hasattr(blob, "value") else blob
        returned_refresh = decrypt_for_user(
            blob=blob_bytes, user_id=github_user_id,
            item_type="oauth_refresh_token",
        )

    return User(
        github_user_id=github_user_id,
        login=login,
        role=item["role"],
        tier=item["tier"],
        allowlisted=bool(item["allowlisted"]),
        oauth_access_token=oauth_access_token,
        oauth_refresh_token=returned_refresh,
        created_at=item["created_at"],
        allowlisted_at=item.get("allowlisted_at"),
        allowlisted_by=item.get("allowlisted_by"),
    )
