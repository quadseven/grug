"""GitHub App auth — JWT signing + install token exchange (cached).

Per PRD #21 Q17: warm-container module-scope cache. Cold start re-signs.
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt

from ports.token_cache import InMemoryTokenCache

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.github_app_auth")

_cache = InMemoryTokenCache()
_GH_API = "https://api.github.com"
_JWT_TTL_SECONDS = 9 * 60  # GitHub allows up to 10min; refresh at 9min

# grug#946: bounded retry for a transient GitHub outage. 5 attempts at
# base=1s/factor=2 is 1+2+4+8=15s of backoff before the 5th and final
# try - long enough to ride out a short blip, short enough that a
# sustained outage (2026-08-17: tens of minutes of GraphQL 503s) still
# fails in well under a minute and falls back to the existing durable
# redrive/DLQ machinery instead of blocking a worker.
_RETRY_MAX_ATTEMPTS = 5
_RETRY_BASE_SECONDS = 1.0
_RETRY_BACKOFF_FACTOR = 2.0
# GitHub's own secondary-rate-limit guidance: honor Retry-After literally
# rather than out-guessing it with exponential backoff, but still capped -
# an uncapped Retry-After would let GitHub's response dictate how long a
# worker blocks.
_RETRY_MAX_SLEEP_SECONDS = 30.0
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
_RATE_LIMIT_STATUS = frozenset({403, 429})


def _app_id() -> str:
    from secrets_loader import _get_ssm_secure_string  # type: ignore
    # Strip: unlike several other SSM reads in secrets_loader.py, this raw
    # fetch has no normalization - a stray trailing newline in the SSM
    # parameter would make every performed_via_github_app.id string
    # comparison in _find_marker_comment (#554/#560/#561) fail forever,
    # silently duplicating marker comments instead of matching our own
    # (LORE review, PR #694).
    return _get_ssm_secure_string(os.environ["GITHUB_APP_ID_SSM"]).strip()


def get_app_id() -> str:
    """Public accessor for our OWN app's numeric GitHub App ID - needed
    beyond JWT signing to verify a webhook comment's `performed_via_
    github_app.id` is genuinely OURS, not merely "some GitHub App"
    (#554 peer review round 3, codex: a decoy comment from a DIFFERENT
    installed app would otherwise pass a bare non-null check)."""
    return _app_id()


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


def get_install_token(installation_id: int, *, force_refresh: bool = False) -> str:
    """Return a fresh installation access token (cached up to ~55min).

    GitHub installation tokens last 1hr; cache for 55min to leave skew.
    Pass `force_refresh=True` after observing a 401 from GitHub to skip
    the cache (Codex post-review #50). Use `with_install_token_retry`
    instead of calling this directly when wrapping API calls.
    """
    key = f"install_token:{installation_id}"
    if force_refresh:
        _cache.invalidate(key)
    else:
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
    # raise_for_status() validates only the HTTP status, not the body schema.
    # A 200 can still carry a truncated body, a gateway interstitial that
    # parses as JSON-without-`token`, or an unexpected schema. Guard the parse
    # + key access so this hot path fails with an actionable typed error and a
    # structured log line instead of a bare KeyError/ValueError opaque 500
    # (which, on the webhook side, makes GitHub retry the delivery). Mirrors
    # the defensive parsing in llm_client._parse_envelope. The response body is
    # NOT logged (it may contain a token-shaped value).
    try:
        payload = resp.json()
        token = payload["token"]
    except (ValueError, KeyError, TypeError) as e:
        log.warning(
            "install_token_exchange_malformed_response",
            extra={"installation_id": installation_id, "error": type(e).__name__},
        )
        raise RuntimeError(
            "GitHub returned a 200 without a usable installation token "
            f"(installation {installation_id}): {type(e).__name__}"
        ) from e
    # GitHub returns expires_at ISO; default 1hr from creation.
    _cache.put(key, token, ttl_seconds=55 * 60)
    return token


def get_scoped_install_token(
    installation_id: int,
    *,
    repositories: list[str],
    permissions: dict[str, str],
) -> str:
    """Mint a fresh installation token DOWN-SCOPED to specific repositories +
    permissions (#469). GitHub's token-create API narrows a token when the body
    carries `repositories` / `permissions` subsets - the resulting token can do
    strictly LESS than the installation's full grant. Used to hand the Smasher
    Trial sandbox a `contents:read`-only, single-repo token (ADR-0013).

    NOT cached: a scoped token is minted per Trial for a one-shot clone and must
    never be reused as if it were the full-scope cached token. The response body
    is never logged (it carries a token-shaped value)."""
    resp = httpx.post(
        f"{_GH_API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {get_app_jwt()}",
            "Accept": "application/vnd.github+json",
        },
        json={"repositories": repositories, "permissions": permissions},
        timeout=10,
    )
    resp.raise_for_status()
    try:
        return resp.json()["token"]
    except (ValueError, KeyError, TypeError) as e:
        log.warning(
            "scoped_install_token_malformed_response",
            extra={"installation_id": installation_id, "error": type(e).__name__},
        )
        raise RuntimeError(
            "GitHub returned a 200 without a usable scoped installation token "
            f"(installation {installation_id}): {type(e).__name__}"
        ) from e


def _is_secondary_rate_limit(response: httpx.Response) -> bool:
    """GitHub's secondary rate limit is a 403/429 that is NOT the primary
    per-hour limit. The primary limit carries `X-RateLimit-Remaining: 0`;
    the secondary one is everything else in that status range - a
    `Retry-After` header, or a body naming it, per GitHub's own docs.
    Treated as retryable either way: both mean "slow down", not "denied"."""
    if response.headers.get("Retry-After"):
        return True
    try:
        text = response.text.lower()
    except Exception:
        return False
    return "secondary rate limit" in text or "abuse detection" in text


def _is_retryable_github_error(response: httpx.Response) -> bool:
    status = response.status_code
    if status in _RETRYABLE_STATUS:
        return True
    if status in _RATE_LIMIT_STATUS:
        return _is_secondary_rate_limit(response)
    return False


def _retry_sleep_seconds(attempt: int, response: httpx.Response) -> float:
    """Exponential backoff + jitter, but a server-supplied `Retry-After`
    wins when it asks for longer - GitHub's guidance for its own
    secondary rate limit is to honor that value, not out-guess it.
    Jitter avoids every retrying worker waking on the same tick."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            server_wait = float(retry_after)
        except ValueError:
            server_wait = 0.0
    else:
        server_wait = 0.0
    backoff = _RETRY_BASE_SECONDS * (_RETRY_BACKOFF_FACTOR ** (attempt - 1))
    jitter = random.uniform(0, backoff * 0.5)  # noqa: S311 - retry timing jitter, not a security use
    return min(max(backoff + jitter, server_wait), _RETRY_MAX_SLEEP_SECONDS)


_APP_INSTALLATIONS_MAX_PAGES = 10  # 1000 installations - generous cap, log + truncate never spin


def list_app_installations() -> list[dict]:
    """Enumerate every installation of THIS GitHub App (`GET /app/installations`,
    paginated, App-JWT authed - not `with_install_token_retry`: an install
    missing from our own store has no cached install token to retry with,
    which is exactly the gap this exists to find, grug#842).

    Returns GitHub's raw installation objects (`id`, `account`, ...) - no
    field here identifies WHO installed it (that only ever arrives on the
    `installation.created` webhook's `sender`), so a caller repairing a
    missing store row from this list has no real `installed_by_user_id` to
    give `record_installation`. Page cap mirrors `list_installation_repos`:
    log + truncate, never spin.
    """
    out: list[dict] = []
    for page in range(1, _APP_INSTALLATIONS_MAX_PAGES + 1):
        resp = httpx.get(
            f"{_GH_API}/app/installations",
            params={"per_page": 100, "page": page},
            headers={
                "Authorization": f"Bearer {get_app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        )
        resp.raise_for_status()
        page_items = resp.json() or []
        if not page_items:
            break
        out.extend(page_items)
    return out


def _emit_github_api_result(ok: bool) -> None:
    """grug#948: a DENSE error gauge (1.0 = this call ended in error after
    exhausting retries, 0.0 = it succeeded), emitted on EVERY call through
    `with_install_token_retry` - never only on the bad case.
    `grug.check_publish.transient_retries_exhausted` (publish_check.py)
    only ever fires on failure, which is fine for a one-off "the budget
    ran out" ping but cannot answer "what fraction of calls are failing" -
    that needs the successes in the same series too, or a quiet healthy
    stretch and a quiet fully-broken stretch look identical to a monitor.
    Same reasoning as `emit_enforcement_metric` (ADR-0022): threshold the
    VALUE, never gate emission on the outcome. Best-effort; telemetry must
    never affect the retry path itself."""
    try:
        from observability import emit_gauge  # type: ignore
        emit_gauge("grug.github_api.error", 0.0 if ok else 1.0)
    except Exception:  # noqa: BLE001 - telemetry never breaks a GitHub call
        pass


def with_install_token_retry(installation_id: int, fn):
    """Run `fn(token)`, retrying transient failures.

    On httpx 401, invalidate the cached token and retry once - GitHub
    revokes tokens out-of-band on App reinstall, perm change, or secret
    rotation, and the long-lived process would otherwise reuse the bad
    cached token until the 55-min TTL elapsed (Codex post-review #50).

    On a 5xx or secondary rate limit (grug#946 - the 2026-08-17 GraphQL
    degradation aborted an in-flight review outright instead of riding
    out a transient blip), retry with bounded exponential backoff and
    jitter, honoring a server `Retry-After` when it asks for longer.
    Any other 4xx (permission denied, not found, unprocessable) is
    PERMANENT - the identical request would fail identically forever, so
    retrying it only wastes the same budget grug#770 exists to protect.
    """
    token = get_install_token(installation_id)
    refreshed_401 = False
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            result = fn(token)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 401 and not refreshed_401:
                refreshed_401 = True
                token = get_install_token(installation_id, force_refresh=True)
                continue
            if attempt < _RETRY_MAX_ATTEMPTS and _is_retryable_github_error(e.response):
                sleep_seconds = _retry_sleep_seconds(attempt, e.response)
                log.warning(
                    "github_api_retry",
                    extra={
                        "installation_id": installation_id,
                        "status": status,
                        "attempt": attempt,
                        "sleep_seconds": round(sleep_seconds, 2),
                    },
                )
                time.sleep(sleep_seconds)
                continue
            _emit_github_api_result(False)
            raise
        else:
            _emit_github_api_result(True)
            return result
