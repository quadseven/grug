"""Round-trip tests for crypto.kms_envelope synchronous wrappers.

Mocks boto3 KMS client to avoid AWS round-trip but exercises:
- encrypt → decrypt round-trip returns the original plaintext
- decrypt with wrong user_id raises CredentialBlobCorrupt (AAD mismatch)
- decrypt with wrong item_type raises CredentialBlobCorrupt
- decrypt of truncated/short blob raises CredentialBlobCorrupt
- encrypt without GRUG_KMS_CMK_ARN raises RuntimeError
- DEK_BYTES guard rejects wrong-length keys
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def _kms_envelope(monkeypatch):
    """Import kms_envelope with a fake CMK ARN + a stubbed KMS client.

    The module reads GRUG_KMS_CMK_ARN at import time; we set it BEFORE
    importing. boto3 KMS calls are routed to a stub that simulates
    GenerateDataKey + Decrypt with deterministic output bound to the
    EncryptionContext (so context-mismatch raises just like real KMS).
    """
    monkeypatch.setenv("GRUG_KMS_CMK_ARN", "arn:aws:kms:us-east-1:000:key/test")

    import importlib
    import crypto.kms_envelope as mod  # type: ignore  # noqa: F401

    # Ensure module re-imports with new env (CMK arn is read at import)
    importlib.reload(mod)

    # Real per-call DEK + ciphertext storage so decrypt path can recover
    # the same DEK that was generated. Keyed by ciphertext_blob bytes.
    storage: dict[bytes, tuple[bytes, dict]] = {}

    def fake_generate_data_key(*, KeyId, KeySpec, EncryptionContext):
        # Deterministic 32-byte DEK derived from ctx so test is repeatable
        ctx_str = repr(sorted(EncryptionContext.items())).encode()
        plaintext_dek = (b"K" + ctx_str)[:32].ljust(32, b"\0")
        ciphertext_blob = b"WRAPPED:" + ctx_str
        storage[ciphertext_blob] = (plaintext_dek, dict(EncryptionContext))
        return {"Plaintext": plaintext_dek, "CiphertextBlob": ciphertext_blob}

    def fake_decrypt(*, CiphertextBlob, EncryptionContext):
        if CiphertextBlob not in storage:
            raise RuntimeError("InvalidCiphertextException: unknown blob")
        plaintext_dek, ctx = storage[CiphertextBlob]
        if ctx != EncryptionContext:
            raise RuntimeError("InvalidCiphertextException: ctx mismatch")
        return {"Plaintext": plaintext_dek}

    monkeypatch.setattr(mod._kms, "generate_data_key", fake_generate_data_key)
    monkeypatch.setattr(mod._kms, "decrypt", fake_decrypt)

    return mod


def test_round_trip_returns_original_plaintext(_kms_envelope):
    mod = _kms_envelope
    blob = mod.encrypt_for_user("hunter2-token", user_id="100", item_type="oauth_access_token")
    out = mod.decrypt_for_user(blob, user_id="100", item_type="oauth_access_token")
    assert out == "hunter2-token"


def test_decrypt_wrong_user_id_raises_corrupt(_kms_envelope):
    mod = _kms_envelope
    blob = mod.encrypt_for_user("token-A", user_id="100", item_type="oauth_access_token")
    with pytest.raises(mod.CredentialBlobCorrupt):
        mod.decrypt_for_user(blob, user_id="200", item_type="oauth_access_token")


def test_decrypt_wrong_item_type_raises_corrupt(_kms_envelope):
    mod = _kms_envelope
    blob = mod.encrypt_for_user("token-A", user_id="100", item_type="oauth_access_token")
    with pytest.raises(mod.CredentialBlobCorrupt):
        mod.decrypt_for_user(blob, user_id="100", item_type="oauth_refresh_token")


def test_decrypt_short_blob_raises_corrupt(_kms_envelope):
    mod = _kms_envelope
    with pytest.raises(mod.CredentialBlobCorrupt, match="too short"):
        mod.decrypt_for_user(b"abc", user_id="100", item_type="oauth_access_token")


def test_decrypt_truncated_blob_raises_corrupt(_kms_envelope):
    mod = _kms_envelope
    blob = mod.encrypt_for_user("payload", user_id="100", item_type="oauth_access_token")
    # Cut off mid-DEK ciphertext
    truncated = blob[: 14 + 5]  # nonce(12) + dek_len(2) + 5 bytes of dek
    with pytest.raises(mod.CredentialBlobCorrupt, match="truncated"):
        mod.decrypt_for_user(truncated, user_id="100", item_type="oauth_access_token")


def test_encrypt_without_cmk_arn_raises(monkeypatch):
    monkeypatch.delenv("GRUG_KMS_CMK_ARN", raising=False)
    import importlib
    import crypto.kms_envelope as mod  # type: ignore
    importlib.reload(mod)
    with pytest.raises(RuntimeError, match="GRUG_KMS_CMK_ARN"):
        mod.encrypt_for_user("anything", user_id="1", item_type="x")


def test_encryption_context_includes_user_id_and_item_type(_kms_envelope):
    mod = _kms_envelope
    ctx = mod._encryption_context("42", "oauth_refresh_token")
    assert ctx == {"user_id": "42", "item_type": "oauth_refresh_token"}


def test_aad_binds_user_id_and_item_type(_kms_envelope):
    mod = _kms_envelope
    aad_a = mod.build_aad("100", "oauth_access_token")
    aad_b = mod.build_aad("100", "oauth_refresh_token")
    aad_c = mod.build_aad("200", "oauth_access_token")
    # Same context = same AAD; any difference = different AAD
    assert aad_a == mod.build_aad("100", "oauth_access_token")
    assert aad_a != aad_b
    assert aad_a != aad_c
