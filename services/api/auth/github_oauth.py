"""GitHub OAuth flow for grug-api (Slice 3 #24).

3 endpoints:
  GET /api/v1/auth/github/login       — redirect to GitHub authorize URL
  GET /api/v1/auth/github/callback    — code → user-token + DDB upsert
  GET /api/v1/me                      — current user (always 200 even
                                        for non-allowlisted; SPA reads
                                        `allowlisted` to route to /waitlist)

CSRF state: HMAC of a server secret + timestamp; stateless verify on
callback. No session table needed.

Rate limits (#1680): login and callback are each capped per client IP
per minute through the Postgres rate-limit store. Settings:
  GRUG_OAUTH_LOGIN_PER_MINUTE     (default 20) - GET .../login
  GRUG_OAUTH_CALLBACK_PER_MINUTE  (default 10) - GET .../callback
Past the cap the route answers 429 with Retry-After set to the seconds
left in the minute window. The callback check runs before the CSRF state
check, so junk codes count too. The client IP is CF-Connecting-IP when
present (the API sits behind Cloudflare), else the socket peer. A store
fault fails open with a warning rather than locking every user out.

Per Slice 5 (#26) the allowlist gate is enforced via FastAPI dependency
on routes other than /me + /waitlist. v1 short-circuits non-allowlisted
users at /api/v1/installations/* etc.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlencode

import httpx
import psycopg
from adapters.pg_rate_limit_store import try_reserve_slot
from adapters.user_store import get_user, upsert_oauth_user
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import RedirectResponse

log = logging.getLogger("grug.api.auth")

router = APIRouter(prefix="/api/v1")

_GH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GH_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GH_USER_URL = "https://api.github.com/user"
_DOMAIN = os.environ.get("GRUG_DOMAIN", "grug.lol")
# All authed users land on /dashboard regardless of allowlist state —
# the dashboard renders the awaiting-allowlist banner inline (Slice 7).
# Earlier callback split allowlisted vs not into /dashboard vs /waitlist
# but the SPA never had a /waitlist route → 404. Codex post-review #55.
_DASHBOARD_URL = f"https://{_DOMAIN}/dashboard"
_STATE_TTL_SECONDS = 600  # 10 min
# Per-client-IP caps, see the module docstring (#1680).
_LOGIN_PER_MINUTE = int(os.getenv("GRUG_OAUTH_LOGIN_PER_MINUTE", "20"))
_CALLBACK_PER_MINUTE = int(os.getenv("GRUG_OAUTH_CALLBACK_PER_MINUTE", "10"))


# A resolved signing key, cached for the life of the process. Only a
# key that every pod resolves the same way is cached: the dedicated key,
# or the webhook key when the dedicated one is not configured or not
# provisioned. A transient read failure is never cached (#1627).
_signing_secret_cache: str | None = None
_signing_secret_lock = threading.Lock()


class SessionSigningUnavailable(HTTPException):
    """The dedicated signing key could not be read; the request fails closed."""

    def __init__(self) -> None:
        super().__init__(status_code=503, detail="session_signing_unavailable")


def _is_parameter_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    return isinstance(error, dict) and error.get("Code") == "ParameterNotFound"


def _state_secret() -> str:
    """HMAC key for CSRF state + session-cookie signing.

    Prefers a DEDICATED secret (GRUG_SESSION_SIGNING_SECRET_SSM) so that
    session/CSRF signing does NOT share a key with GitHub webhook
    verification (audit #5). Sharing was a latent account-takeover lever:
    one leaked secret -> forge a session cookie for any gh_id (incl. admin),
    and you could not rotate the webhook secret to respond without breaking
    inbound webhook verification + logging every user out at once.

    Resolution:
    - Dedicated read succeeds: cache it for the process.
    - Dedicated env var unset (local dev), or the parameter does not exist
      yet (ParameterNotFound, the zero-downtime migration path in
      docs/HITL_PREREQUISITES.md): use the webhook secret with a warning.
      Every pod sees the same answer, so this is cached too.
    - Any other dedicated read failure (throttle, network, access): raise
      SessionSigningUnavailable (503) and cache nothing, so the next
      request retries. Falling back here would let one pod sign with a
      different key than its peers for its whole lifetime (#1627).
    """
    global _signing_secret_cache
    cached = _signing_secret_cache
    if cached is not None:
        return cached
    with _signing_secret_lock:
        if _signing_secret_cache is not None:
            return _signing_secret_cache
        _signing_secret_cache = _resolve_signing_secret()
        return _signing_secret_cache


def _resolve_signing_secret() -> str:
    from botocore.exceptions import BotoCoreError, ClientError
    from secrets_loader import _get_ssm_secure_string  # type: ignore
    dedicated = os.environ.get("GRUG_SESSION_SIGNING_SECRET_SSM", "")
    if dedicated:
        try:
            return _get_ssm_secure_string(dedicated)
        except (ClientError, BotoCoreError) as e:
            # Only SSM read faults land here. A bug in the loader (NameError,
            # AttributeError) propagates as itself instead of posing as an
            # SSM outage behind a 503.
            # NB: do NOT log `dedicated` (the SSM param name) - the only
            # such param is GRUG_SESSION_SIGNING_SECRET_SSM, so the message
            # already identifies it, and logging a value sourced from a
            # `*_SECRET_SSM` env var trips clear-text-secret scanners.
            if not _is_parameter_not_found(e):
                log.error(
                    "session_signing_secret_read_failed",
                    extra={"kind": type(e).__name__},
                )
                raise SessionSigningUnavailable() from e
            log.warning("session_signing_secret_unprovisioned_using_fallback")
    else:
        log.warning("session_signing_secret_unconfigured_using_fallback")
    return _get_ssm_secure_string(
        os.environ.get("GITHUB_APP_WEBHOOK_SECRET_SSM", "")
    )


def _make_state() -> str:
    """`<random>.<ts>.<hmac(random+ts)>` — stateless CSRF token."""
    rand = secrets.token_urlsafe(16)
    ts = str(int(time.time()))
    sig = hmac.new(
        _state_secret().encode(), f"{rand}.{ts}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{rand}.{ts}.{sig}"


def _verify_state(state: str) -> bool:
    parts = state.split(".")
    if len(parts) != 3:
        return False
    rand, ts, sig = parts
    expected = hmac.new(
        _state_secret().encode(), f"{rand}.{ts}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    if int(time.time()) - int(ts) > _STATE_TTL_SECONDS:
        return False
    return True


# Session token format = `<rand>.<ts>.<gh_id>.<hmac(rand.ts.gh_id)>`.
# Critical: `gh_id` is in the signed payload — without that, a holder of
# any valid session can swap the trailing component to impersonate any
# user. (Codex P1 review of Slice 7 #28.)
_SESSION_TTL_SECONDS = 86400 * 7  # 7 days


def _make_session(gh_id: str) -> str:
    rand = secrets.token_urlsafe(16)
    ts = str(int(time.time()))
    sig = hmac.new(
        _state_secret().encode(),
        f"{rand}.{ts}.{gh_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{rand}.{ts}.{gh_id}.{sig}"


def _verify_session(token: str) -> str | None:
    """Returns the GitHub user id on success, None on any failure."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    rand, ts, gh_id, sig = parts
    expected = hmac.new(
        _state_secret().encode(),
        f"{rand}.{ts}.{gh_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        if int(time.time()) - int(ts) > _SESSION_TTL_SECONDS:
            return None
    except ValueError:
        return None
    return gh_id


def _client_id() -> str:
    from secrets_loader import _get_ssm_secure_string  # type: ignore
    name = os.environ.get("GITHUB_APP_CLIENT_ID_SSM", "")
    return _get_ssm_secure_string(name)


def _client_secret() -> str:
    from secrets_loader import _get_ssm_secure_string  # type: ignore
    name = os.environ.get("GITHUB_APP_CLIENT_SECRET_SSM", "")
    return _get_ssm_secure_string(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _client_ip(request: Request) -> str:
    """CF-Connecting-IP when present, else the socket peer."""
    forwarded = request.headers.get("cf-connecting-ip", "").strip()
    if forwarded:
        return forwarded
    if request.client is not None:
        return request.client.host
    return "unknown"


def _enforce_ip_limit(kind: Literal["login", "callback"], request: Request) -> None:
    """Admit one request for this client IP or raise 429 with Retry-After.

    Only the minute window binds: the store also keeps a day counter, so
    the day cap is set to a full day of minute caps and never trips first.
    """
    minute_limit = _LOGIN_PER_MINUTE if kind == "login" else _CALLBACK_PER_MINUTE
    ip = _client_ip(request)
    now = _now()
    try:
        slot = try_reserve_slot(
            name=f"oauth_{kind}:{ip}",
            minute_limit=minute_limit,
            day_limit=minute_limit * 1440,
            now=now,
        )
    except (psycopg.Error, OSError) as e:
        log.warning(
            "oauth_rate_limit_store_unavailable",
            extra={"route": kind, "kind": type(e).__name__},
        )
        return
    if slot.admitted:
        return
    retry_after = max(1, 60 - int(now.timestamp() % 60))
    log.info(
        "oauth_rate_limited",
        extra={"route": kind, "client_ip": ip, "minute_count": slot.minute_count},
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="rate_limited",
        headers={"Retry-After": str(retry_after)},
    )


def _limit_login(request: Request) -> None:
    _enforce_ip_limit("login", request)


def _limit_callback(request: Request) -> None:
    _enforce_ip_limit("callback", request)


# The limits are route dependencies, so FastAPI runs them before the
# handler body: the callback is counted before its CSRF state check.
@router.get("/auth/github/login", dependencies=[Depends(_limit_login)])
def login() -> RedirectResponse:
    """302 to GitHub authorize URL."""
    state = _make_state()
    params = {
        "client_id": _client_id(),
        "redirect_uri": f"https://api.{_DOMAIN}/api/v1/auth/github/callback",
        "state": state,
        # GitHub Apps don't use scopes — installation permissions handle
        # this. We DO want a user-access token for "act on user's behalf"
        # (e.g. write to .github/grug.yml later). Empty scope = identity only.
        "scope": "",
    }
    resp = RedirectResponse(
        url=f"{_GH_AUTHORIZE_URL}?{urlencode(params)}",
        status_code=302,
    )
    resp.set_cookie(
        "grug_oauth_state", state,
        max_age=_STATE_TTL_SECONDS, httponly=True, secure=True, samesite="lax",
    )
    return resp


@router.get("/auth/github/callback", dependencies=[Depends(_limit_callback)])
def callback(
    code: str = Query(...),
    state: str = Query(...),
    grug_oauth_state: str = Cookie(default=""),
) -> RedirectResponse:
    """OAuth callback — exchange code → user-token + upsert DDB.

    Handler is `def`, NOT `async def` — every downstream call is sync
    httpx + sync boto3 KMS + sync DDB. FastAPI runs sync defs in a
    threadpool so concurrent requests don't starve each other. Don't
    flip this to `async def` without converting the httpx + boto3
    paths to their async equivalents (re-eval at v1.5 if fan-out
    behavior lands). See main.py:60-67 + RUNBOOK 'Sync-vs-async
    route handlers'. Closes async-blocker-hunter F-03.
    """
    # CSRF
    if not state or state != grug_oauth_state or not _verify_state(state):
        raise HTTPException(status_code=400, detail="invalid_state")

    # Exchange code → user-access-token. Catch BOTH HTTPStatusError
    # (4xx/5xx from GitHub) AND RequestError (transport: timeout, DNS,
    # connection-reset). Without the RequestError catch, a transient
    # network blip surfaces as a 500 with no actionable log line —
    # async-blocker-hunter F-02.
    try:
        token_resp = httpx.post(
            _GH_TOKEN_URL,
            data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "code": code,
                "redirect_uri": f"https://api.{_DOMAIN}/api/v1/auth/github/callback",
            },
            headers={"Accept": "application/json"},
            timeout=10,
        )
        token_resp.raise_for_status()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "oauth_token_endpoint_failed",
            extra={"err": str(e), "kind": type(e).__name__},
        )
        raise HTTPException(status_code=502, detail="oauth_upstream_failed") from e
    try:
        token_payload = token_resp.json()
    except ValueError as e:
        # A 200 that is not JSON (an interstitial page, a gateway error
        # body) is an upstream failure, not a 500 of ours.
        log.warning("oauth_token_endpoint_not_json", extra={"kind": type(e).__name__})
        raise HTTPException(status_code=502, detail="oauth_upstream_failed") from e
    if not isinstance(token_payload, dict):
        log.warning("oauth_token_endpoint_not_json", extra={"kind": type(token_payload).__name__})
        raise HTTPException(status_code=502, detail="oauth_upstream_failed")
    access_token = token_payload.get("access_token")
    refresh_token = token_payload.get("refresh_token")
    if not access_token:
        log.warning("oauth_token_missing", extra={"err": token_payload.get("error")})
        raise HTTPException(status_code=400, detail="oauth_token_exchange_failed")

    # Identity — same transport-error wrap. F-02.
    try:
        user_resp = httpx.get(
            _GH_USER_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        user_resp.raise_for_status()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "oauth_user_endpoint_failed",
            extra={"err": str(e), "kind": type(e).__name__},
        )
        raise HTTPException(status_code=502, detail="oauth_upstream_failed") from e
    gh_user = user_resp.json()
    # Defensive: GH can return {} or partial body if the access_token
    # was revoked between the token-exchange call and this user-info
    # call. KeyError → 500 with no log. silent-failure-hunter P2 #5.
    raw_id = gh_user.get("id")
    login_name = gh_user.get("login")
    if not raw_id or not login_name:
        log.warning(
            "oauth_user_payload_missing_fields",
            extra={"keys": list(gh_user.keys())},
        )
        raise HTTPException(status_code=502, detail="oauth_user_malformed")
    gh_id = str(raw_id)

    user = upsert_oauth_user(
        github_user_id=gh_id,
        login=login_name,
        oauth_access_token=access_token,
        oauth_refresh_token=refresh_token,
    )
    log.info(
        "oauth_callback_success",
        extra={"github_user_id": gh_id, "login": login_name,
               "allowlisted": user.allowlisted, "role": user.role},
    )

    # Set session cookie. Format: rand.ts.gh_id.sig where sig HMAC-binds
    # gh_id (else the trailing component is swappable; Codex P1 in Slice 7).
    session_value = _make_session(gh_id)
    target = _DASHBOARD_URL  # Codex post-review #55 — see _DASHBOARD_URL note above.
    resp = RedirectResponse(url=target, status_code=302)
    resp.delete_cookie("grug_oauth_state")
    resp.set_cookie(
        "grug_session", session_value,
        max_age=_SESSION_TTL_SECONDS,
        httponly=True, secure=True, samesite="lax",
    )
    return resp


@router.get("/me")
def me(grug_session: str = Cookie(default="")) -> dict[str, str | bool]:
    """Current user. Always 200 (auth'd or anon). SPA routes by `allowlisted`."""
    gh_id = _verify_session(grug_session)
    if gh_id is None:
        return {"authenticated": False}

    user = get_user(gh_id)
    if not user:
        return {"authenticated": False}

    return {
        "authenticated": True,
        "github_user_id": user.github_user_id,
        "login": user.login,
        "role": user.role,
        "tier": user.tier,
        "allowlisted": user.allowlisted,
    }


@router.post("/auth/logout")
def logout() -> Response:
    resp = Response(status_code=204)
    resp.delete_cookie("grug_session")
    return resp
