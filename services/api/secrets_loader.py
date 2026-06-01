# MIRRORED — sibling at services/webhook/secrets_loader.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
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


def get_openrouter_api_key() -> str:
    name = os.getenv("GRUG_OPENROUTER_API_KEY_SSM", "")
    return _get_ssm_secure_string(name)


def get_poolside_api_key() -> str:
    name = os.getenv("GRUG_POOLSIDE_API_KEY_SSM", "")
    return _get_ssm_secure_string(name)


@lru_cache(maxsize=1)
def get_prompt_experiment_mode() -> str:
    """The Elder prompt-A/B experiment mode (#191), from the
    `/grug/elder-prompt-experiment` SSM param (plain String). One of
    `off` (all installs → v1), `split` (orthogonal-to-backend per-install
    v1/v2), or `all_v2`. Cached per warm container.

    FALLBACK-SAFE: returns `off` (the safe default — everyone on the
    shipped v1 prompt) on a missing/unreadable param or any SSM error.
    The experiment must never break a review (the #253 lesson: a missing
    SSM param should degrade, not raise). `no redeploy` switching takes
    effect on the next cold start / container recycle (the cache is
    warm-container-scoped)."""
    name = os.getenv("GRUG_PROMPT_EXPERIMENT_SSM", "")
    if not name:
        return "off"
    try:
        resp = _ssm.get_parameter(Name=name)
        return resp["Parameter"]["Value"]
    except Exception as e:  # noqa: BLE001 — best-effort config; never break review
        log.warning(
            "prompt_experiment_mode_fetch_failed",
            extra={"param": name, "kind": type(e).__name__},
        )
        return "off"
