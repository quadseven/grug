# MIRRORED — sibling at services/api/secrets_loader.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""SSM SecureString loader with module-scope cache.

Lambda warm container reuses the same SSM-fetched secret across
invocations. Cold start pays the SSM round-trip; warm invocations are
in-memory.

Per PRD #21: no plaintext-secret caching across cold starts (i.e. no
DDB-backed cache for plaintext). Module-scope is the in-process cache;
when the container recycles, we fetch fresh from SSM.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

import boto3

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.secrets")
_ssm = boto3.client("ssm")


@lru_cache(maxsize=8)
def _get_ssm_secure_string(name: str) -> str:
    """Fetch a SecureString SSM parameter, cached per warm container."""
    if not name:
        raise RuntimeError("SSM parameter name is empty — env not configured")
    resp = _ssm.get_parameter(Name=name, WithDecryption=True)
    return resp["Parameter"]["Value"]


def get_webhook_secret() -> str:
    name = os.getenv("GITHUB_APP_WEBHOOK_SECRET_SSM", "")
    return _get_ssm_secure_string(name)


def get_app_id() -> str:
    name = os.getenv("GITHUB_APP_ID_SSM", "")
    return _get_ssm_secure_string(name)


def get_app_private_key() -> str:
    name = os.getenv("GITHUB_APP_PRIVATE_KEY_SSM", "")
    return _get_ssm_secure_string(name)
