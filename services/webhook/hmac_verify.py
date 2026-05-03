"""HMAC SHA-256 signature verifier for GitHub App webhooks.

Pure function — no IO, no globals. Single responsibility: given the
shared secret + raw body bytes + signature header value, return True iff
the signature matches.

GitHub format: `X-Hub-Signature-256: sha256=<hex>` — see
https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries

Constant-time comparison via `hmac.compare_digest` to prevent timing
side-channels on signature length.
"""

from __future__ import annotations

import hashlib
import hmac


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Return True iff the signature header matches HMAC-SHA256(body, secret).

    Returns False (not raises) for any malformed input — empty header,
    wrong prefix, wrong length, mismatched digest. The caller decides how
    to surface failure (HTTP status, log, etc).
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    if not secret:
        # An empty secret would HMAC-match a forged "sha256=<hex of empty>"
        # — refuse outright rather than computing and comparing.
        return False

    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    provided = signature_header.removeprefix("sha256=")

    return hmac.compare_digest(expected, provided)
