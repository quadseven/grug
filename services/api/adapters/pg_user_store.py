"""Postgres user store - exact-API port of user_store.py (DDB).

api-only (the webhook never touches user rows beyond the read in
install_store.is_install_allowlisted). KMS envelope encryption is
UNCHANGED - this module stores/returns the same opaque encrypted blobs
(bytes ride the jsonb column via pg_base's b64 sentinel codec).

Also exposes the three store operations admin.py previously performed
with raw `_table` access (scan_meta_items / get_user_item /
update_user_fields) so the route module stops reaching into storage
internals at cutover.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg

from adapters.pg_base import TTL_LIVE, decode_item, encode_attrs, get_pool

log = logging.getLogger("grug.api.adapters.pg_user_store")


@dataclass(frozen=True)
class UserIdentity:
    """Identity-only projection of a user row. NO token material."""

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
    """Identity + decrypted OAuth tokens (OAuth callback/refresh only)."""

    identity: UserIdentity
    oauth_access_token: str
    oauth_refresh_token: str | None


def _user_pk(github_user_id: str) -> str:
    return f"USER#{github_user_id}"


def _fetch_item(github_user_id: str) -> dict[str, Any] | None:
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                f"SELECT pk, sk, data FROM grug_kv "
                f"WHERE pk = %s AND sk = 'META' AND {TTL_LIVE}",
                (_user_pk(github_user_id),),
            ).fetchone()
    except psycopg.Error as e:
        # Same contract as the DDB adapter: surface infrastructure
        # failures distinctly from a legitimate miss, or a transient DB
        # error would spuriously log the user out (silent-failure P1 #2).
        log.error(
            "user_store_get_item_failed",
            extra={"github_user_id": github_user_id, "code": type(e).__name__},
        )
        raise
    if not row:
        return None
    return decode_item(row[0], row[1], row[2])


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
    item = _fetch_item(github_user_id)
    if not item:
        return None
    return _identity_from_item(github_user_id, item)


def delete_user_state(github_user_id: str) -> None:
    """Idempotently REMOVE only the credential blobs (spec 0005
    PurgeCorrupt): identity fields are PRESERVED - an encryption failure
    must not strip an admin's privileges or a paid user's tier."""
    with get_pool().connection() as conn:
        conn.execute(
            """
            UPDATE grug_kv
            SET data = data - 'oauth_access_token_blob' - 'oauth_refresh_token_blob'
            WHERE pk = %s AND sk = 'META'
            """,
            (_user_pk(github_user_id),),
        )


def get_user_with_tokens(github_user_id: str) -> UserWithTokens | None:
    """Identity + decrypted OAuth tokens, or None. KMS Decrypt happens
    here; CredentialBlobCorrupt triggers the documented opportunistic
    purge + clean miss (user re-auths)."""
    from crypto.kms_envelope import CredentialBlobCorrupt, decrypt_for_user  # lazy

    item = _fetch_item(github_user_id)
    if not item:
        return None

    try:
        encrypted_token = item.get("oauth_access_token_blob")
        if encrypted_token:
            blob = (
                encrypted_token.value
                if hasattr(encrypted_token, "value")
                else encrypted_token
            )
            access_token = decrypt_for_user(
                blob=blob, user_id=github_user_id, item_type="oauth_access_token"
            )
        else:
            access_token = ""

        encrypted_refresh = item.get("oauth_refresh_token_blob")
        refresh_token: str | None = None
        if encrypted_refresh:
            blob = (
                encrypted_refresh.value
                if hasattr(encrypted_refresh, "value")
                else encrypted_refresh
            )
            refresh_token = decrypt_for_user(
                blob=blob, user_id=github_user_id, item_type="oauth_refresh_token"
            )
    except CredentialBlobCorrupt as exc:
        log.error(
            "credential_blob_corrupt_purging_row",
            extra={"github_user_id": github_user_id, "reason": str(exc)},
        )
        try:
            delete_user_state(github_user_id)
        except psycopg.Error as purge_exc:
            log.error(
                "credential_blob_corrupt_purge_failed",
                extra={
                    "github_user_id": github_user_id,
                    "purge_code": type(purge_exc).__name__,
                    "original_reason": str(exc),
                },
            )
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
    """Atomic create-or-update from the OAuth callback flow.

    Single-statement upsert: identity fields (role/tier/allowlisted/
    created_at) default on first sight and are PRESERVED on re-auth -
    no read-then-write window in which an admin-side change could be
    silently reverted (lost-update anomaly, peer-review CRITICAL 4x on
    the DDB original). An existing refresh blob is preserved when GitHub
    re-auth returns access without rotating refresh (Sentry HIGH #39).
    """
    from crypto.kms_envelope import encrypt_for_user  # lazy import

    now = datetime.now(timezone.utc).isoformat()
    encrypted_access = encrypt_for_user(
        plaintext=oauth_access_token,
        user_id=github_user_id,
        item_type="oauth_access_token",
    )

    always: dict[str, Any] = {
        "login": login,
        "oauth_access_token_blob": encrypted_access,
        "last_login_at": now,
    }
    if oauth_refresh_token:
        always["oauth_refresh_token_blob"] = encrypt_for_user(
            plaintext=oauth_refresh_token,
            user_id=github_user_id,
            item_type="oauth_refresh_token",
        )
    defaults: dict[str, Any] = {
        "role": "user",
        "tier": "free",
        "allowlisted": False,
        "created_at": now,
    }

    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data)
            VALUES (%(pk)s, 'META', %(defaults)s || %(always)s)
            ON CONFLICT (pk, sk) DO UPDATE
                SET data = (%(defaults)s || grug_kv.data) || %(always)s
            RETURNING pk, sk, data
            """,
            {
                "pk": _user_pk(github_user_id),
                "defaults": encode_attrs(defaults),
                "always": encode_attrs(always),
            },
        ).fetchone()
    item = decode_item(row[0], row[1], row[2])

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


# ---------------------------------------------------------------------------
# Admin support (previously raw `_table` access inside admin.py)
# ---------------------------------------------------------------------------


def scan_meta_items(*, pk_prefix: str) -> list[dict[str, Any]]:
    """All live META rows whose PK starts with `pk_prefix` (USER#/INST#).

    The DDB version paginated a 1MB-page Scan with a 50-page defensive
    cap; a single indexed WHERE replaces it.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT pk, sk, data FROM grug_kv
            WHERE sk = 'META' AND pk LIKE %s AND {TTL_LIVE}
            """,
            (pk_prefix.replace("%", r"\%").replace("_", r"\_") + "%",),
        ).fetchall()
    return [decode_item(*r) for r in rows]


def get_user_item(user_id: str) -> dict[str, Any] | None:
    """Raw user item for admin views (includes audit fields)."""
    return _fetch_item(str(user_id))


def update_user_fields(user_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Merge `fields` into an EXISTING user row; returns the new item.

    Admin-only path; the caller has already 404'd on a missing row, but
    enforce it here too (the WHERE) so a race with row deletion cannot
    resurrect a sparse row.

    No TTL_LIVE in the WHERE (unlike the read paths): USER# META rows
    never carry a ttl - only install-store claim/comment rows do - so
    the asymmetry is intentional, not a missed filter. Same applies to
    delete_user_state.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            UPDATE grug_kv SET data = data || %(fields)s
            WHERE pk = %(pk)s AND sk = 'META'
            RETURNING pk, sk, data
            """,
            {"pk": _user_pk(str(user_id)), "fields": encode_attrs(fields)},
        ).fetchone()
    if row is None:
        raise LookupError(f"user row vanished during update: {user_id}")
    return decode_item(row[0], row[1], row[2])
