# MIRRORED from services/pasto-api/kms_envelope.py — keep in sync.
# Tempo + Macro Chef share the platform primitives (Cognito, KMS, DDB,
# middleware, observability) per the Allegro umbrella architecture
# (PRD #217 / decision-log Tempo bootstrap entry). Future PR extracts
# these into services/_shared/ once a third Allegro sub-app lands
# (rule-of-three threshold).
#
"""KMS envelope encryption (CI-M-002-001 / DL-003 / DL-016 / DL-024 / DL-032).

Per-user data-encryption keys (DEK) wrapped by the customer-managed CMK.
Every kms.GenerateDataKey / kms.Decrypt call passes
``EncryptionContext={user_id, item_type}`` so a CloudTrail audit binds
each operation to its logical blob class and a tampered context (e.g.
swapping the user_id) raises ``InvalidCiphertextException`` rather than
silently returning bytes for the wrong user.

The same ``{user_id, item_type}`` map is also bound as AES-GCM
``associated_data`` so a (nonce, ciphertext) tuple from one user/item
cannot be transplanted onto another row (the tag check fails).

Plaintext DEKs never live longer than a single function call; the
per-request, no-cache KMS Decrypt pattern is binding (DL-032) — cache
invalidation under serverless is its own fault domain.

References: DL-003, DL-016, DL-024, DL-032, R-013.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    from types_aiobotocore_kms.client import KMSClient

log = logging.getLogger("grug.api.kms_envelope")


# `GRUG_KMS_CMK_ARN` is required at cold start. Missing CMK ARN is a
# deploy-time misconfiguration, not a runtime fallback — fail loud at
# module import.
KMS_CMK_ARN = os.environ.get("GRUG_KMS_CMK_ARN", "")

# AES-GCM nonce: 96 bits per NIST SP 800-38D §8.2.1 — use a random nonce
# per encrypt; collision risk for 2^32 nonces under a single key is
# ~2^-32, well under our per-user key write rate.
_NONCE_LEN = 12
_DEK_BYTES = 32  # AES_256


class CredentialBlobCorrupt(Exception):
    """Raised when AES-GCM tag fails or KMS rejects the EncryptionContext.

    Caller maps to HTTP 409 'credential_blob_corrupt' so the UI can surface
    the reauth banner.
    """


class UserStateCorrupt(Exception):
    """Raised when a persisted UserState row cannot be decoded.

    Caller maps to HTTP 503 so the UI does not silently overwrite a
    corrupt row with a fresh empty UserState.
    """


def _encryption_context(user_id: str, item_type: str) -> dict[str, str]:
    """Build the EncryptionContext map. Both fields are mandatory per DL-016."""
    if not user_id:
        raise ValueError("user_id is required for EncryptionContext")
    if not item_type:
        raise ValueError("item_type is required for EncryptionContext")
    return {"user_id": user_id, "item_type": item_type}


def build_aad(user_id: str, item_type: str) -> bytes:
    """Compute AES-GCM associated_data from the EncryptionContext.

    Sort keys so encrypt + decrypt agree byte-for-byte regardless of
    Python dict ordering quirks.
    """
    return json.dumps(
        {"user_id": user_id, "item_type": item_type},
        sort_keys=True,
    ).encode("utf-8")


async def generate_user_dek(
    kms_client: KMSClient,
    user_id: str,
    item_type: str,
    *,
    cmk_arn: str | None = None,
) -> tuple[bytes, bytes]:
    """Mint a per-user DEK via ``kms.GenerateDataKey``.

    Returns ``(plaintext_dek, ciphertext_blob)``. The caller MUST encrypt
    the target blob with the plaintext key and then drop it; the
    ciphertext_blob is what persists in DDB at SK=``creds`` alongside the
    AES-GCM nonce + ciphertext (DL-024).
    """
    arn = cmk_arn or KMS_CMK_ARN
    if not arn:
        raise RuntimeError("KMS_CMK_ARN is required (DL-017)")
    resp = await kms_client.generate_data_key(
        KeyId=arn,
        KeySpec="AES_256",
        EncryptionContext=_encryption_context(user_id, item_type),
    )
    return resp["Plaintext"], resp["CiphertextBlob"]


async def decrypt_user_dek(
    kms_client: KMSClient,
    user_id: str,
    item_type: str,
    ciphertext_blob: bytes,
) -> bytes:
    """Unwrap a per-user DEK via ``kms.Decrypt``.

    A mismatched ``EncryptionContext`` (e.g. swapping user_id) raises
    ``InvalidCiphertextException`` at the KMS API layer — the caller
    propagates it. Plaintext DEK is returned for immediate AES-GCM use
    and MUST be dropped immediately after.
    """
    resp = await kms_client.decrypt(
        CiphertextBlob=ciphertext_blob,
        EncryptionContext=_encryption_context(user_id, item_type),
    )
    return resp["Plaintext"]


def encrypt_blob(plaintext_dict: dict, dek: bytes, aad: bytes) -> tuple[bytes, bytes]:
    """AES-GCM encrypt a JSON-serialisable dict.

    ``aad`` is bound as ``associated_data`` so a (nonce, ciphertext)
    tuple from one user/item cannot be transplanted onto another row.
    Returns ``(nonce, ciphertext)`` for separate DDB storage. JSON
    serialisation uses ``sort_keys=True`` so the ciphertext is
    deterministic given the same input dict + DEK + nonce.
    """
    if len(dek) != _DEK_BYTES:
        raise ValueError(f"dek must be {_DEK_BYTES} bytes (got {len(dek)})")
    nonce = os.urandom(_NONCE_LEN)
    aesgcm = AESGCM(dek)
    serialised = json.dumps(plaintext_dict, sort_keys=True).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, serialised, associated_data=aad)
    return nonce, ciphertext


def decrypt_blob(ciphertext: bytes, nonce: bytes, dek: bytes, aad: bytes) -> dict:
    """Inverse of ``encrypt_blob``.

    Raises whatever ``cryptography`` raises (typically
    ``InvalidTag``) on tamper / wrong DEK / truncation / AAD mismatch.
    Caller catches and maps to ``CredentialBlobCorrupt``.
    """
    if len(dek) != _DEK_BYTES:
        raise ValueError(f"dek must be {_DEK_BYTES} bytes (got {len(dek)})")
    aesgcm = AESGCM(dek)
    serialised = aesgcm.decrypt(nonce, ciphertext, associated_data=aad)
    return json.loads(serialised.decode("utf-8"))


# ─── grug-specific synchronous wrappers (PRD #21) ─────────────────────
#
# The mirror's API is async + dict-payload (designed for credential
# blobs that hold multiple fields). grug stores single-string OAuth
# tokens via a sync boto3 client. These helpers wrap GenerateDataKey +
# AES-GCM into one call each. Plaintext DEK still drops on function exit.
#
# Wire format (single bytes blob): nonce(12) || dek_ciphertext_len(2 BE) ||
#                                   dek_ciphertext || ct
import boto3 as _boto3

_kms = _boto3.client("kms")


def encrypt_for_user(plaintext: str, user_id: str, item_type: str) -> bytes:
    """Sync wrapper: GenerateDataKey + AES-GCM encrypt str → opaque bytes."""
    if not KMS_CMK_ARN:
        raise RuntimeError("GRUG_KMS_CMK_ARN env var required at cold start")
    aad = build_aad(user_id, item_type)
    resp = _kms.generate_data_key(
        KeyId=KMS_CMK_ARN,
        KeySpec="AES_256",
        EncryptionContext=_encryption_context(user_id, item_type),
    )
    plaintext_dek = resp["Plaintext"]
    dek_ct = resp["CiphertextBlob"]
    nonce = os.urandom(_NONCE_LEN)
    aesgcm = AESGCM(plaintext_dek)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=aad)
    dek_len = len(dek_ct)
    if dek_len > 0xFFFF:
        raise ValueError("DEK ciphertext too large for 2-byte length prefix")
    return nonce + dek_len.to_bytes(2, "big") + dek_ct + ct


def decrypt_for_user(blob: bytes, user_id: str, item_type: str) -> str:
    """Inverse of encrypt_for_user. Raises CredentialBlobCorrupt on tamper."""
    aad = build_aad(user_id, item_type)
    if len(blob) < _NONCE_LEN + 2:
        raise CredentialBlobCorrupt("blob too short to contain nonce + dek_len")
    nonce = blob[:_NONCE_LEN]
    dek_len = int.from_bytes(blob[_NONCE_LEN : _NONCE_LEN + 2], "big")
    dek_start = _NONCE_LEN + 2
    dek_end = dek_start + dek_len
    if len(blob) < dek_end:
        raise CredentialBlobCorrupt("blob truncated within dek_ciphertext")
    dek_ct = blob[dek_start:dek_end]
    ct = blob[dek_end:]

    try:
        decrypt_resp = _kms.decrypt(
            CiphertextBlob=dek_ct,
            EncryptionContext=_encryption_context(user_id, item_type),
        )
    except Exception as exc:
        raise CredentialBlobCorrupt(f"KMS Decrypt failed: {exc}") from exc

    plaintext_dek = decrypt_resp["Plaintext"]
    aesgcm = AESGCM(plaintext_dek)
    try:
        plaintext = aesgcm.decrypt(nonce, ct, associated_data=aad)
    except Exception as exc:
        raise CredentialBlobCorrupt(f"AES-GCM decrypt failed: {exc}") from exc
    return plaintext.decode("utf-8")
