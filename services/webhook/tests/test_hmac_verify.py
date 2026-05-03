"""Unit tests for the HMAC verifier.

Pure-function tests — no fixtures, no IO, no AWS. Run via:
    cd services/webhook && pytest tests/
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from hmac_verify import verify_signature


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


SECRET = "test-secret-do-not-use-in-prod"
BODY = b'{"action":"opened","number":42}'


def test_valid_signature_returns_true() -> None:
    sig = _sign(SECRET, BODY)
    assert verify_signature(SECRET, BODY, sig) is True


def test_tampered_body_returns_false() -> None:
    sig = _sign(SECRET, BODY)
    tampered = BODY + b" extra"
    assert verify_signature(SECRET, tampered, sig) is False


def test_wrong_secret_returns_false() -> None:
    sig = _sign(SECRET, BODY)
    assert verify_signature("different-secret", BODY, sig) is False


def test_missing_header_returns_false() -> None:
    assert verify_signature(SECRET, BODY, "") is False


def test_wrong_prefix_returns_false() -> None:
    sig = _sign(SECRET, BODY).replace("sha256=", "sha1=")
    assert verify_signature(SECRET, BODY, sig) is False


def test_empty_secret_returns_false_even_with_matching_hmac() -> None:
    # An empty secret is a misconfiguration — refuse to verify even
    # against a "valid" empty-secret HMAC, otherwise a forgetful deploy
    # could accept any payload.
    forged = _sign("", BODY)
    assert verify_signature("", BODY, forged) is False


def test_malformed_hex_returns_false() -> None:
    assert verify_signature(SECRET, BODY, "sha256=not-hex") is False


@pytest.mark.parametrize(
    "header",
    [
        "sha256",        # missing '='
        "=abc",          # missing prefix name
        "sha256=",       # empty digest
        "SHA256=abc",    # case-sensitive (per spec, GitHub sends lowercase)
    ],
)
def test_malformed_header_shapes_return_false(header: str) -> None:
    assert verify_signature(SECRET, BODY, header) is False
