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
from typing import Literal, cast, get_args

import boto3

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.secrets")
_ssm = boto3.client("ssm")

# The recognized Elder prompt-experiment arms (#191). A value outside this set
# (operator typo / stray whitespace) is treated as "off" — same safe default as
# a missing param — but is LOGGED (unlike the silent "off" default) so a
# fat-fingered toggle is distinguishable from an intentional disable.
# `Mode` is the single source: the runtime allow-list is derived from it, so
# adding an arm updates both the type and the validation at once.
Mode = Literal["off", "split", "all_v2"]
_EXPERIMENT_MODES = frozenset(get_args(Mode))


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
def get_prompt_experiment_mode() -> Mode:
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
        # Strip so a console-pasted value with a trailing newline ("split\n")
        # still matches — otherwise it would silently degrade to v1.
        value = resp["Parameter"]["Value"].strip()
    except Exception as e:  # noqa: BLE001 — best-effort config; never break review
        log.warning(
            "prompt_experiment_mode_fetch_failed",
            extra={"param": name, "kind": type(e).__name__},
        )
        return "off"
    if value not in _EXPERIMENT_MODES:
        # Fetched fine, but the value is not a known arm — an operator typo.
        # Degrade to "off" (control) but LOG it: otherwise the operator sees
        # 100% v1 in DD and can't tell a typo'd "split" from an intentional
        # disable (the silent-failure trap silent-failure-hunter flagged).
        log.warning(
            "prompt_experiment_mode_unrecognized",
            extra={"param": name, "mode": value},
        )
        return "off"
    # Narrowed: `value` passed the `_EXPERIMENT_MODES` (== get_args(Mode)) gate.
    return cast("Mode", value)


def get_fallback_enabled() -> bool:
    """Whether the Elder cave-fallback (ADR-0005) is enabled, from the
    `/grug/elder-fallback-enabled` SSM param (plain String, e.g. "true").

    FALLBACK-SAFE in the strongest sense: returns `False` on a missing /
    unreadable param, an SSM error, or any unrecognized value. The fallback
    must never turn ITSELF on by accident (enqueuing to a queue whose connector
    isn't live yet would just pile messages up), and a config blip must never
    break a review. Read only on the rare `all_failed` path, so it is
    intentionally NOT cached — toggling takes effect without a container
    recycle."""
    name = os.getenv("GRUG_FALLBACK_ENABLED_SSM", "")
    if not name:
        return False
    try:
        value = _ssm.get_parameter(Name=name)["Parameter"]["Value"].strip().lower()
    except Exception as e:  # noqa: BLE001 — best-effort config; never break a review
        log.warning(
            "fallback_enabled_fetch_failed",
            extra={"param": name, "kind": type(e).__name__},
        )
        return False
    return value in ("true", "1", "yes", "on")
