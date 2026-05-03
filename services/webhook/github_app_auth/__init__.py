"""GitHub App auth — JWT signing + install token exchange (cached).

Per PRD #21 Q17: warm-container module-scope cache. Cold start re-signs.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt

from ports.token_cache import InMemoryTokenCache, TokenCache

_cache: TokenCache = InMemoryTokenCache()
_GH_API = "https://api.github.com"
_JWT_TTL_SECONDS = 9 * 60  # GitHub allows up to 10min; refresh at 9min


def _app_id() -> str:
    from secrets_loader import _get_ssm_secure_string  # type: ignore
    return _get_ssm_secure_string(os.environ["GITHUB_APP_ID_SSM"])


def _app_private_key() -> str:
    from secrets_loader import _get_ssm_secure_string  # type: ignore
    return _get_ssm_secure_string(os.environ["GITHUB_APP_PRIVATE_KEY_SSM"])


def get_app_jwt() -> str:
    """Return a fresh App JWT (cached up to ~9min)."""
    cached = _cache.get("app_jwt")
    if cached:
        return cached.value
    now = datetime.now(timezone.utc)
    payload = {
        "iat": int((now - timedelta(seconds=60)).timestamp()),  # 60s clock skew
        "exp": int((now + timedelta(seconds=_JWT_TTL_SECONDS)).timestamp()),
        "iss": _app_id(),
    }
    token = jwt.encode(payload, _app_private_key(), algorithm="RS256")
    _cache.put("app_jwt", token, _JWT_TTL_SECONDS - 30)
    return token


def get_install_token(installation_id: int) -> str:
    """Return a fresh installation access token (cached up to ~55min).

    GitHub installation tokens last 1hr; cache for 55min to leave skew.
    On 401 (token revoked / rotated mid-cycle), caller catches + retries.
    """
    key = f"install_token:{installation_id}"
    cached = _cache.get(key)
    if cached:
        return cached.value

    resp = httpx.post(
        f"{_GH_API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {get_app_jwt()}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload["token"]
    # GitHub returns expires_at ISO; default 1hr from creation.
    _cache.put(key, token, ttl_seconds=55 * 60)
    return token
