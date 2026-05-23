"""DDB user store with KMS envelope encryption for OAuth tokens.

Single-table layout (per PRD #21):
  PK = USER#<github_user_id>
  SK = META

oauth_access_token + oauth_refresh_token are encrypted via the KMS
envelope (services/api/crypto/kms_envelope.py) BEFORE write. DDB sees
opaque ciphertext; CloudTrail logs every kms.Decrypt with the
EncryptionContext bound (anti-row-transplant defense).

Type split (issue #103): callers that don't need OAuth tokens get a
`UserIdentity` — never carries plaintext token material. Callers that
do (OAuth refresh path, future App-on-behalf-of-user calls) explicitly
opt in via `UserWithTokens` + the `_with_tokens` getter. Both types are
`frozen=True` so a stray `user.role = 'admin'` write fails loudly.
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
# Thread-safety via double-checked locking: a warm Lambda handling two
# concurrent invocations could race the unguarded check; both would call
# boto3.resource() and one of the two resource handles would leak. The
# lock + re-check after acquire is the canonical fix and adds zero cost
# after the first call (the outer `is None` short-circuits without
# touching the lock). Peer-review HIGH (openrouter).
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


@dataclass(frozen=True)
class UserIdentity:
    """Identity-only projection of a user row. NO token material.

    Default for `Depends(require_authenticated)` so a stray
    `log.info("user", extra=user.__dict__)` cannot leak a plaintext
    OAuth token. Routes that need tokens use `UserWithTokens` + the
    explicit `get_user_with_tokens` / `require_authenticated_with_tokens`
    getter.
    """

    github_user_id: str
    login: str
    role: str  # "admin" | "user"
    tier: str  # "lifetime" | "free" | "paid"
    allowlisted: bool
    created_at: str
    allowlisted_at: str | None
    allowlisted_by: str | None


@dataclass(frozen=True)
class UserWithTokens:
    """Identity + decrypted OAuth tokens. Used only on the OAuth callback
    + token-refresh paths. KMS Decrypt happened during construction.
    """

    identity: UserIdentity
    oauth_access_token: str
    oauth_refresh_token: str | None


def _user_pk(github_user_id: str) -> str:
    return f"USER#{github_user_id}"


def _fetch_item(github_user_id: str) -> dict[str, Any] | None:
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
    return resp.get("Item")


def _identity_from_item(github_user_id: str, item: dict[str, Any]) -> UserIdentity:
    return UserIdentity(
        github_user_id=github_user_id,
        login=item.get("login", ""),
        role=item.get("role", "user"),
        tier=item.get("tier", "free"),
        allowlisted=bool(item.get("allowlisted", False)),
        created_at=item.get("created_at", ""),
        allowlisted_at=item.get("allowlisted_at"),
        allowlisted_by=item.get("allowlisted_by"),
    )


def get_user(github_user_id: str) -> UserIdentity | None:
    """Return identity-only user row, or None.

    No KMS Decrypt — token material is never read on this path. Use
    `get_user_with_tokens` for OAuth-refresh / on-behalf-of-user paths.
    """
    item = _fetch_item(github_user_id)
    if not item:
        return None
    return _identity_from_item(github_user_id, item)


def delete_user_state(github_user_id: str) -> None:
    """Idempotently remove the user row from DDB.

    Used by the spec 0005 `KmsEnvelope.PurgeCorrupt` audit pattern:
    when `decrypt_for_user` raises `CredentialBlobCorrupt` (key-version
    drift, tampering, AAD mismatch), the encrypted blob is unrecoverable
    and the row must go so the next OAuth login can repopulate cleanly.
    """
    _table.delete_item(Key={"PK": _user_pk(github_user_id), "SK": "META"})


def get_user_with_tokens(github_user_id: str) -> UserWithTokens | None:
    """Return identity + decrypted OAuth tokens, or None.

    KMS Decrypt happens here (no plaintext caching per the envelope
    contract). Restricted to OAuth-refresh + on-behalf-of-user routes.

    `CredentialBlobCorrupt` is the documented escape hatch: per spec 0005
    `credential_blob_corrupt_triggers_idempotent_cleanup_per_persistence_concepts`,
    a corrupt blob triggers row deletion + a clean miss return so the
    next sign-in repopulates. Caller sees `None` and routes to /signin.
    """
    from crypto.kms_envelope import CredentialBlobCorrupt, decrypt_for_user  # lazy import

    item = _fetch_item(github_user_id)
    if not item:
        return None

    try:
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
    except CredentialBlobCorrupt as exc:
        # Spec 0005 PurgeCorrupt: cleanup is idempotent — safe to call
        # repeatedly even if the row is already gone. The corrupt blob
        # is unrecoverable; surface a clean miss + force re-auth.
        log.error(
            "credential_blob_corrupt_purging_row",
            extra={
                "github_user_id": github_user_id,
                "reason": str(exc),
            },
        )
        delete_user_state(github_user_id)
        return None

    return UserWithTokens(
        identity=_identity_from_item(github_user_id, item),
        oauth_access_token=access_token,
        oauth_refresh_token=refresh_token,
    )


def upsert_oauth_user(
    *,
    github_user_id: str,
    login: str,
    oauth_access_token: str,
    oauth_refresh_token: str | None = None,
) -> UserIdentity:
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

    # Return identity only — current callers (OAuth callback) only read
    # role/login/allowlisted. Removed the post-upsert refresh-blob
    # decrypt that earlier callers used to populate the returned User.
    # Token-needing routes use `get_user_with_tokens` explicitly.
    return UserIdentity(
        github_user_id=github_user_id,
        login=login,
        role=item["role"],
        tier=item["tier"],
        allowlisted=bool(item["allowlisted"]),
        created_at=item["created_at"],
        allowlisted_at=item.get("allowlisted_at"),
        allowlisted_by=item.get("allowlisted_by"),
    )
