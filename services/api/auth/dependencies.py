"""FastAPI dependencies for session-cookie auth.

Single source for "who is the current user?" — re-uses the stateless
HMAC session cookie format from `auth/github_oauth.py`.

Layered deps so routes pick the strictness they need:
  - get_current_user                  → UserIdentity | None (for /me-like routes)
  - require_authenticated             → UserIdentity (401 if missing)
  - require_admin                     → UserIdentity (403 if not admin)

No dependency returns decrypted OAuth tokens: no route acts on behalf of
a user today. `get_user_with_tokens` in the user store is the decrypt
path to wire here when one does.

Allowlist-gating happens at the persona/dispatch layer, NOT here —
non-allowlisted users still need to see /dashboard so they can be told
"awaiting allowlist" rather than redirected to a login loop.
"""

from __future__ import annotations

import logging

from fastapi import Cookie, HTTPException, status

from adapters.user_store import (
    UserIdentity,
    get_user,
)
from auth.github_oauth import _verify_session  # type: ignore[reportPrivateUsage]

log = logging.getLogger("grug.api.auth.deps")


def get_current_user(grug_session: str = Cookie(default="")) -> UserIdentity | None:
    """Resolve cookie → UserIdentity. Returns None for anonymous or invalid.

    Session-cookie HMAC binds gh_id (Codex P1 fix in Slice 7) — earlier
    versions left gh_id unsigned and were vulnerable to swap-component
    impersonation.
    """
    gh_id = _verify_session(grug_session)
    if gh_id is None:
        return None
    return get_user(gh_id)


def require_authenticated(grug_session: str = Cookie(default="")) -> UserIdentity:
    user = get_current_user(grug_session)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
        )
    return user


def require_admin(grug_session: str = Cookie(default="")) -> UserIdentity:
    user = require_authenticated(grug_session)
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return user

