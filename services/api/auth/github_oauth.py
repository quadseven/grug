"""GitHub OAuth flow for grug-api (Slice 3 #24).

3 endpoints:
  GET /api/v1/auth/github/login       — redirect to GitHub authorize URL
  GET /api/v1/auth/github/callback    — code → user-token + DDB upsert
  GET /api/v1/me                      — current user (always 200 even
                                        for non-allowlisted; SPA reads
                                        `allowlisted` to route to /waitlist)

CSRF state: HMAC of a server secret + timestamp; stateless verify on
callback. No session table needed.

Per Slice 5 (#26) the allowlist gate is enforced via FastAPI dependency
on routes other than /me + /waitlist. v1 short-circuits non-allowlisted
users at /api/v1/installations/* etc.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Query, Response
from fastapi.responses import RedirectResponse

from adapters.user_store import get_user, upsert_oauth_user

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


def _state_secret() -> str:
    """Reuse the GitHub App webhook secret for CSRF state HMAC.
    Same secret already in SSM; same secret-rotation cadence."""
    from secrets_loader import _get_ssm_secure_string  # type: ignore
    name = os.environ.get("GITHUB_APP_WEBHOOK_SECRET_SSM", "")
    return _get_ssm_secure_string(name)


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


@router.get("/auth/github/login")
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


@router.get("/auth/github/callback")
def callback(
    code: str = Query(...),
    state: str = Query(...),
    grug_oauth_state: str = Cookie(default=""),
) -> RedirectResponse:
    """OAuth callback — exchange code → user-token + upsert DDB."""
    # CSRF
    if not state or state != grug_oauth_state or not _verify_state(state):
        raise HTTPException(status_code=400, detail="invalid_state")

    # Exchange code → user-access-token
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
    token_payload = token_resp.json()
    access_token = token_payload.get("access_token")
    refresh_token = token_payload.get("refresh_token")
    if not access_token:
        log.warning("oauth_token_missing", extra={"err": token_payload.get("error")})
        raise HTTPException(status_code=400, detail="oauth_token_exchange_failed")

    # Identity
    user_resp = httpx.get(
        _GH_USER_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        timeout=10,
    )
    user_resp.raise_for_status()
    gh_user = user_resp.json()
    gh_id = str(gh_user["id"])
    login_name = gh_user["login"]

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
