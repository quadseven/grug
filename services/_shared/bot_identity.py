"""The GitHub App identity grug posts as (#1630).

Every own-comment check (thread resolution, living review, carry-forward,
Sentinel) and the autofix commit author read the App slug from here, so a
self-hosted App under its own slug recognises its own comments.

    GRUG_BOT_SLUG=your-app-slug   # unset or blank = grug-tribe

GraphQL reports an App author as the bare slug; REST and webhook payloads
add the `[bot]` suffix. `is_own_login` accepts both. The env is read on
every call (not cached) so tests and a re-exec see the current value.
"""

from __future__ import annotations

import os

ENV_VAR = "GRUG_BOT_SLUG"
DEFAULT_SLUG = "grug-tribe"
_BOT_SUFFIX = "[bot]"


def bot_slug() -> str:
    """The App slug, without the `[bot]` suffix."""
    return (os.getenv(ENV_VAR) or "").strip() or DEFAULT_SLUG


def bot_login() -> str:
    """The App's REST/webhook login, `<slug>[bot]`."""
    return f"{bot_slug()}{_BOT_SUFFIX}"


def is_own_login(login: str | None) -> bool:
    """True when `login` is this App, in either the GraphQL or REST form.
    GitHub logins are case-insensitive."""
    if not login:
        return False
    return login.removesuffix(_BOT_SUFFIX).lower() == bot_slug().lower()


def commit_identity() -> dict[str, str]:
    """Git author/committer for commits the App makes."""
    login = bot_login()
    return {"name": login, "email": f"{login}@users.noreply.github.com"}
