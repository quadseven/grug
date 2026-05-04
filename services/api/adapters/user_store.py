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

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

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

    resp = _table.get_item(
        Key={"PK": _user_pk(github_user_id), "SK": "META"},
        ConsistentRead=True,
    )
    item = resp.get("Item")
    if not item:
        return None

    encrypted_token = item.get("oauth_access_token_blob")
    if encrypted_token:
        # Boto3 returns DDB Binary as bytes
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

    return User(
        github_user_id=github_user_id,
        login=login,
        role=item["role"],
        tier=item["tier"],
        allowlisted=bool(item["allowlisted"]),
        oauth_access_token=oauth_access_token,
        oauth_refresh_token=oauth_refresh_token,
        created_at=item["created_at"],
        allowlisted_at=item.get("allowlisted_at"),
        allowlisted_by=item.get("allowlisted_by"),
    )
