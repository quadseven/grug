"""Who wrote a pull request's commits, for the gates that must not let the
reviewed vouch for its own review."""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

log = logging.getLogger("grug.pr_commits")

# GitHub's pull request commits endpoint returns at most this many commits,
# so a list this long may be cut short.
_PR_COMMITS_CAP = 250


def _commit_logins(rows: list[dict]) -> set[str] | None:
    """The authors and committers of one page, or None for an unlinked one."""
    logins: set[str] = set()
    for row in rows:
        for role in ("author", "committer"):
            who = str(((row or {}).get(role) or {}).get("login") or "").lower()
            if not who:
                return None
            if who != "web-flow":
                logins.add(who)
    return logins


def pr_commit_logins(
    installation_id: int, owner: str, repo_name: str, pr_number: int,
) -> frozenset[str] | None:
    """Lower-case logins that authored or committed any commit on the PR
    (GitHub's web-flow committer excluded), or None, which refuses a trusted
    operator: the list could not be read, it reached GitHub's 250-commit cap
    on that endpoint (so it may be cut short), or a commit's author or
    committer maps to no GitHub login (an unlinked identity could be anyone).
    Git metadata can still name another person's linked address; push-actor
    proof is out of scope, since the owner chooses whom to trust."""
    from github_app_auth import with_install_token_retry  # type: ignore

    def _read(token: str) -> frozenset[str] | None:
        logins: set[str] = set()
        total = 0
        for page in (1, 2, 3):
            r = httpx.get(
                f"https://api.github.com/repos/{quote(owner, safe='')}/"
                f"{quote(repo_name, safe='')}/pulls/{int(pr_number)}/commits"
                f"?per_page=100&page={page}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10,
            )
            r.raise_for_status()
            rows = r.json() or []
            total += len(rows)
            if total >= _PR_COMMITS_CAP:
                return None
            page_logins = _commit_logins(rows)
            if page_logins is None:
                return None
            logins |= page_logins
            if len(rows) < 100:
                return frozenset(logins)
        return None

    try:
        return with_install_token_retry(int(installation_id), _read)
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "pr_commits_lookup_failed",
            extra={"pr_number": pr_number, "kind": type(e).__name__},
        )
        return None
