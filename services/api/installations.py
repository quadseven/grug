"""User-facing installation + per-repo config endpoints (Slice 7 #28).

All session-cookie-authed (allowlist NOT required — users need to see
their own installs even before admin allowlists them so they know to wait):

  GET  /api/v1/installations
       → INST# rows installed by the current user

  GET  /api/v1/installations/{install_id}/repos
       → repos visible to that install (calls GitHub via install token,
         then merges per-repo config from DDB)

  GET  /api/v1/installations/{install_id}/activity
       → recent Check verdicts (Activity feed, PRD #301), newest-first,
         badge derived server-side; `?verdict=` filter, `limit` cap

  PUT  /api/v1/installations/{install_id}/repos/{repo_id}/config
       → upsert per-repo persona override (e.g. {"tpm_enabled": false})

Authorization model: caller must own the install (sender.id ==
INST#installed_by_user_id). Admins bypass the ownership check.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from adapters.install_store import (
    get_installation,
    get_repo_config,
    list_check_verdicts,
    list_user_installations,
    set_repo_config,
)
from adapters.user_store import UserIdentity
from auth.dependencies import require_authenticated
from github_app_auth import with_install_token_retry
from review_types import verdict as derive_verdict

log = logging.getLogger("grug.api.installations")

router = APIRouter(prefix="/api/v1")

_GH_API = "https://api.github.com"


class RepoConfigPayload(BaseModel):
    """SPA → api PUT /repo/{id}/config payload.

    `extra='forbid'` catches SPA typos (e.g. `tmp_enabled=true`) at
    request-validation time so they 422 rather than silently dropping
    the toggle. type-design-analyzer P3.
    """
    model_config = ConfigDict(extra="forbid")
    tpm_enabled: bool = Field(default=True)


class RerunRequest(BaseModel):
    """SPA → api POST .../rerun payload (#305). Keyed by `repo` ("owner/name",
    what the Activity row carries). `extra='forbid'` 422s typos; `repo` +
    `persona` are pattern-constrained so junk can't reach the queue."""
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")  # owner/name
    pr_number: int
    persona: str = Field(pattern=r"^(elder|code_reviewer|chief|tpm)$")


def _ensure_can_access(install: dict[str, Any], user: UserIdentity) -> None:
    """Caller must own the install OR be admin. Raises 403 otherwise."""
    if user.role == "admin":
        return
    owner_id = install.get("installed_by_user_id", "")
    if str(owner_id) != str(user.github_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not your installation",
        )


@router.get("/installations")
def list_installations(user: UserIdentity = Depends(require_authenticated)) -> dict[str, Any]:
    """Installs owned by the current user (admin sees only own here too;
    cross-user listing is admin-only and lives at /api/v1/admin/users)."""
    items = list_user_installations(user.github_user_id)
    out: list[dict[str, Any]] = []
    for it in items:
        # Defensive: GSI1 row with corrupt/missing PK shouldn't 500
        # the whole list endpoint. Skip + log so we surface the
        # corruption without breaking the user's dashboard.
        # silent-failure-hunter P2 #6.
        pk = it.get("PK", "")
        try:
            install_id = int(pk.split("#", 1)[1])
        except (IndexError, ValueError):
            log.error(
                "list_installations_corrupt_pk",
                extra={"pk": pk, "user": user.login},
            )
            continue
        out.append({
            "install_id": install_id,
            "account_login": it.get("account_login", ""),
            "account_type": it.get("account_type", "User"),
            "installed_at": it.get("installed_at", ""),
        })
    return {"installations": out}


@router.get("/installations/{install_id}/repos")
def list_install_repos(
    install_id: int,
    user: UserIdentity = Depends(require_authenticated),
) -> dict[str, Any]:
    """List repos visible to this install (live from GitHub) merged with
    DDB per-repo config so the SPA can render toggle state.

    Capped at 1000 repos (10 × 100 per page); larger orgs log
    `list_repos_pagination_cap` and silently truncate. Raise the cap
    if any v1 install hits it."""
    install = get_installation(install_id)
    if not install:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="install not found")
    _ensure_can_access(install, user)

    # Wrapped in with_install_token_retry so a 401 from GitHub
    # (App reinstall, perm change, secret rotation revoking the cached
    # token mid-warm-container) invalidates the cache + re-fetches
    # before retry. Otherwise the warm Lambda burns the bad token for
    # up to 55min. Closes #50.
    def _fetch(token: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        with httpx.Client(timeout=10) as client:
            while True:
                resp = client.get(
                    f"{_GH_API}/installation/repositories",
                    headers={
                        "Authorization": f"token {token}",
                        "Accept": "application/vnd.github+json",
                    },
                    params={"per_page": 100, "page": page},
                )
                resp.raise_for_status()
                body = resp.json()
                if "repositories" not in body:
                    # GH returned 200 but malformed payload — distinguish
                    # from "0 repos" so the caller doesn't show an empty
                    # dashboard for what's really upstream broken.
                    # silent-failure-hunter P1 #3.
                    log.error(
                        "gh_install_repos_malformed",
                        extra={"install_id": install_id, "page": page},
                    )
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="gh_upstream_malformed",
                    )
                for r in body["repositories"]:
                    cfg = get_repo_config(install_id, r["id"])
                    out.append({
                        "repo_id": r["id"],
                        "full_name": r["full_name"],
                        "private": r.get("private", False),
                        "default_branch": r.get("default_branch", "main"),
                        "config": cfg,
                    })
                if len(body["repositories"]) < 100:
                    break
                page += 1
                if page > 10:  # 1000 repos is plenty for v1
                    log.warning(
                        "list_repos_pagination_cap",
                        extra={"install_id": install_id, "user": user.login},
                    )
                    break
        return out

    repos = with_install_token_retry(install_id, _fetch)
    return {"repos": repos}


def _toggle_enforcement(
    install_id: int, repo_id: int,
    full_name: str, default_branch: str,
    *, enable: bool,
) -> None:
    """Best-effort enforcement create/delete on persona toggle."""
    from enforcement import ensure_enforcement, remove_enforcement  # type: ignore

    parts = full_name.split("/", 1)
    if len(parts) != 2:
        return
    owner, repo_name = parts
    try:
        if enable:
            with_install_token_retry(
                install_id,
                lambda token: ensure_enforcement(
                    token, owner, repo_name, default_branch, install_id, repo_id,
                ),
            )
        else:
            with_install_token_retry(
                install_id,
                lambda token: remove_enforcement(
                    token, owner, repo_name, install_id, repo_id,
                ),
            )
    except Exception:
        log.warning(
            "enforcement_toggle_failed",
            extra={"install_id": install_id, "repo_id": repo_id,
                   "full_name": full_name, "enable": enable},
            exc_info=True,
        )


@router.get("/installations/{install_id}/activity")
def list_activity(
    install_id: int,
    verdict: str | None = None,
    limit: int = 50,
    user: UserIdentity = Depends(require_authenticated),
) -> dict[str, Any]:
    """Activity feed (PRD #301): the install's recent Check verdicts,
    newest-first. The `verdict` badge is DERIVED server-side from each row's
    raw facts via the single `review_types.verdict` mapper (ADR-0003) — the
    frontend renders it verbatim, never re-derives, so a mapping change heals
    history on read. Optional `?verdict=` filters to one badge; `limit` caps
    the result (default 50, clamped 1..200)."""
    install = get_installation(install_id)
    if not install:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="install not found")
    _ensure_can_access(install, user)
    # Clamp the client-controlled limit in-function (not via Query(ge/le)) so
    # the route stays directly callable in unit tests — the repo's endpoint
    # tests invoke route fns directly, where a Query() default wouldn't resolve.
    limit = max(1, min(limit, 200))
    # Fetch the FULL (TTL-bounded) partition — the store materializes + sorts it
    # all regardless — so the re-derive + `?verdict=` filter run across every
    # row before we cap. Capping the fetch would let a sparse filter under-return
    # (matches stranded past the cap). A time-ordered GSI is the noted scale
    # upgrade if an install ever outgrows a single in-memory partition load.
    fetched = list_check_verdicts(install_id, limit=None)
    out: list[dict[str, Any]] = []
    for r in fetched:
        v = derive_verdict(
            conclusion=r["conclusion"],
            findings_count=r["findings_count"],
            degraded_reason=r.get("degraded_reason"),
        )
        if verdict is not None and v != verdict:
            continue
        out.append({
            "persona": r["persona"],
            "repo": r["repo"],
            "pr_number": r["pr_number"],
            "head_sha": r["head_sha"],
            "verdict": v,
            "summary": r["summary"],
            "findings_count": r["findings_count"],
            "created_at": r["created_at"],
        })
        if len(out) >= limit:
            break
    return {"activity": out}


@router.post(
    "/installations/{install_id}/rerun",
    status_code=status.HTTP_202_ACCEPTED,
)
def rerun_check(
    install_id: int,
    body: RerunRequest,
    user: UserIdentity = Depends(require_authenticated),
) -> dict[str, str]:
    """Re-run one persona's check on a PR (#305, ADR-0004). Enqueues to
    `grug-rerun-jobs` and returns 202; the webhook re-runs on the PR's CURRENT
    head and upserts the verdict (heal-in-place / append). Caller must own the
    install. The re-run acts with the INSTALL's GitHub token, so it can only
    touch repos that install can reach (the bound on a user-supplied repo)."""
    install = get_installation(install_id)
    if not install:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="install not found")
    _ensure_can_access(install, user)

    from rerun import enqueue_rerun

    try:
        enqueue_rerun(
            install_id=install_id,
            repo=body.repo,
            pr_number=body.pr_number,
            persona=body.persona,
        )
    except RuntimeError as e:
        log.error("rerun_enqueue_misconfigured", extra={"detail": str(e)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="re-run queue not available",
        ) from e
    return {"status": "queued"}


@router.post(
    "/installations/{install_id}/rerun-all",
    status_code=status.HTTP_202_ACCEPTED,
)
def rerun_all_errored(
    install_id: int,
    user: UserIdentity = Depends(require_authenticated),
) -> dict[str, int]:
    """Re-run EVERY currently-`errored` row for an install (#306) — the
    outage-recovery move ("Elder failed on 12 PRs → one click"). Fans each
    DISTINCT `(repo, pr, persona)` errored row into `grug-rerun-jobs`; the
    per-install FIFO `MessageGroupId` paces the batch (no concurrent LLM burst)
    and content-dedup drops any row already in-flight from a single re-run.
    Returns the count queued. The verdict is re-derived server-side (ADR-0003),
    so the set heals if the verdict mapping changes."""
    install = get_installation(install_id)
    if not install:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="install not found")
    _ensure_can_access(install, user)

    from rerun import enqueue_rerun

    seen: set[tuple[str, int, str]] = set()
    queued = 0
    for r in list_check_verdicts(install_id, limit=None):
        v = derive_verdict(
            conclusion=r["conclusion"],
            findings_count=r["findings_count"],
            degraded_reason=r.get("degraded_reason"),
        )
        if v != "errored":
            continue
        key = (r["repo"], int(r["pr_number"]), r["persona"])
        if key in seen:  # same (repo,pr,persona) across heads → one job (dedup)
            continue
        seen.add(key)
        try:
            enqueue_rerun(
                install_id=install_id,
                repo=r["repo"],
                pr_number=int(r["pr_number"]),
                persona=r["persona"],
            )
        except RuntimeError as e:
            log.error("rerun_all_enqueue_misconfigured", extra={"detail": str(e)})
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="re-run queue not available",
            ) from e
        queued += 1
    return {"queued": queued}


@router.put("/installations/{install_id}/repos/{repo_id}/config")
def update_repo_config(
    install_id: int,
    repo_id: int,
    body: RepoConfigPayload,
    user: UserIdentity = Depends(require_authenticated),
) -> dict[str, Any]:
    """Upsert per-repo persona toggle. Caller must own the install."""
    install = get_installation(install_id)
    if not install:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="install not found")
    _ensure_can_access(install, user)

    # Verify repo really belongs to the install — stops a caller from
    # setting overrides for repos they can't reach.
    #
    # Sentry CRITICAL on PR #43: earlier check used `GET /repositories/{id}`
    # which returns 200 for ANY public repo regardless of install access.
    # Now enumerate via `GET /installation/repositories` (the dedicated
    # endpoint that lists ONLY this install's repos) and verify
    # membership.
    # Wrapped in with_install_token_retry — see list_repos above.
    def _lookup(token: str) -> tuple[bool, str, str]:
        page = 1
        with httpx.Client(timeout=10) as client:
            while True:
                resp = client.get(
                    f"{_GH_API}/installation/repositories",
                    headers={
                        "Authorization": f"token {token}",
                        "Accept": "application/vnd.github+json",
                    },
                    params={"per_page": 100, "page": page},
                )
                resp.raise_for_status()
                body_json = resp.json()
                if "repositories" not in body_json:
                    log.error(
                        "gh_install_repos_malformed",
                        extra={"install_id": install_id, "page": page,
                               "context": "update_repo_config"},
                    )
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="gh_upstream_malformed",
                    )
                for r in body_json["repositories"]:
                    if r["id"] == repo_id:
                        return True, r["full_name"], r.get("default_branch", "main")
                if len(body_json["repositories"]) < 100:
                    return False, "", ""
                page += 1
                # No page cap — single-repo membership lookup must scan
                # all pages on large org installs (>1000 repos). Codex
                # P2 follow-up to the Sentry CRITICAL fix. Worst case
                # ~Npages*100ms; acceptable for an admin write path.

    found, full_name, default_branch = with_install_token_retry(install_id, _lookup)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="repo not visible to install")

    previous_cfg = get_repo_config(install_id, repo_id)
    cfg = set_repo_config(
        install_id=install_id, repo_id=repo_id,
        repo_full_name=full_name, tpm_enabled=body.tpm_enabled,
        updated_by_user_id=user.github_user_id,
    )
    log.info(
        "repo_config_updated",
        extra={
            "install_id": install_id, "repo_id": repo_id,
            "full_name": full_name, "by_user": user.login,
            **cfg,
        },
    )

    was_enabled = previous_cfg.get("tpm_enabled", True)
    now_enabled = body.tpm_enabled
    if now_enabled and not was_enabled:
        _toggle_enforcement(install_id, repo_id, full_name, default_branch, enable=True)
    elif was_enabled and not now_enabled:
        _toggle_enforcement(install_id, repo_id, full_name, default_branch, enable=False)

    return {"repo_id": repo_id, "full_name": full_name, "config": cfg}


@router.get("/installations/{install_id}/repos/{repo_id}/enforcement")
def get_enforcement(
    install_id: int,
    repo_id: int,
    user: UserIdentity = Depends(require_authenticated),
) -> dict[str, Any]:
    """Live enforcement detection — queries GitHub Rulesets + legacy BP."""
    install = get_installation(install_id)
    if not install:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="install not found")
    _ensure_can_access(install, user)

    from github_rulesets_client import detect_enforcement  # type: ignore
    from enforcement import GRUG_DOR_CHECK_NAME  # type: ignore

    def _detect(token: str) -> dict[str, Any]:
        repos = _resolve_repo(token, install_id, repo_id)
        if not repos:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="repo not found")
        full_name, default_branch = repos
        owner, repo_name = full_name.split("/", 1)
        state = detect_enforcement(token, owner, repo_name, default_branch, GRUG_DOR_CHECK_NAME)
        return {"repo_id": repo_id, "enforcement_state": state}

    try:
        return with_install_token_retry(install_id, _detect)
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        # Resilient fallback (dashboard 429 storm). GitHub rate-limited the
        # LIVE detection even after the client's bounded jittered retries.
        # Rather than 500 — or let the UI render a false "⚠ not enforced" off
        # a missing answer — degrade to the last-known STORED state: if Grug
        # has a stored ruleset id it created, report grug_managed; otherwise
        # we genuinely don't know → "unknown" (the badge shows "checking…",
        # never a false warning). `degraded` flags it for the client + DD.
        cfg = get_repo_config(install_id, repo_id)
        stored_id = cfg.get("enforcement_ruleset_id")
        fallback_state = "grug_managed" if stored_id is not None else "unknown"
        log.warning(
            "enforcement_detect_fallback",
            extra={
                "install_id": install_id,
                "repo_id": repo_id,
                "kind": type(e).__name__,
                "status": getattr(getattr(e, "response", None), "status_code", None),
                "fallback_state": fallback_state,
            },
        )
        return {"repo_id": repo_id, "enforcement_state": fallback_state, "degraded": True}


@router.post("/installations/{install_id}/repos/{repo_id}/enforcement")
def fix_enforcement(
    install_id: int,
    repo_id: int,
    user: UserIdentity = Depends(require_authenticated),
) -> dict[str, Any]:
    """Create Grug-managed enforcement ruleset (the "Fix" button)."""
    install = get_installation(install_id)
    if not install:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="install not found")
    _ensure_can_access(install, user)

    from enforcement import ensure_enforcement  # type: ignore

    def _fix(token: str) -> dict[str, Any]:
        repos = _resolve_repo(token, install_id, repo_id)
        if not repos:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="repo not found")
        full_name, default_branch = repos
        owner, repo_name = full_name.split("/", 1)
        state = ensure_enforcement(token, owner, repo_name, default_branch, install_id, repo_id)
        return {"repo_id": repo_id, "enforcement_state": state}

    try:
        return with_install_token_retry(install_id, _fix)
    except httpx.HTTPStatusError as e:
        # GitHub rejected the ruleset create/update (e.g. 422 validation:
        # duplicate name, invalid rule, or the App lacks administration:write).
        # Surface an ACTIONABLE error instead of a raw 500 — the old behavior
        # made the dashboard "fix" button silently do nothing. The GitHub body
        # carries the reason; pass a trimmed version through to the client.
        gh_status = e.response.status_code
        detail = (e.response.text or "")[:300]
        log.warning(
            "fix_enforcement_github_rejected",
            extra={"install_id": install_id, "repo_id": repo_id,
                   "gh_status": gh_status, "detail": detail},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub rejected the ruleset ({gh_status}): {detail}",
        ) from e
    except httpx.RequestError as e:
        # Transport-level failure (DNS, connect/read timeout, connection
        # reset) reaching GitHub, even after with_install_token_retry's
        # bounded retries. Unlike HTTPStatusError this carries NO response -
        # GitHub is unreachable, not rejecting - so the old code let it
        # propagate as an opaque 500 (#331). Return 503 (retryable) with a
        # structured log so the dashboard "Fix" button shows an actionable
        # "try again" and DD can alert. Mirrors get_enforcement's
        # (HTTPStatusError, RequestError) handling on this same resource.
        log.warning(
            "fix_enforcement_transport_error",
            extra={"install_id": install_id, "repo_id": repo_id,
                   "kind": type(e).__name__, "err": str(e)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub unreachable while applying enforcement; please retry.",
        ) from e


def _resolve_repo(
    token: str, install_id: int, repo_id: int,
) -> tuple[str, str] | None:
    """Find repo full_name + default_branch from the installation's repos.

    Capped at 10 pages (1000 repos) — same limit as `_fetch` — so a
    large install can't exhaust the api Lambda's 15s budget on a repo
    not found in its first N pages. Logs a warning when the cap is hit
    so an over-large install is visible in DD.
    """
    with httpx.Client(timeout=10) as client:
        page = 1
        while True:
            resp = client.get(
                f"{_GH_API}/installation/repositories",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            body = resp.json()
            for r in body.get("repositories", []):
                if r["id"] == repo_id:
                    return r["full_name"], r.get("default_branch", "main")
            if len(body.get("repositories", [])) < 100:
                return None
            page += 1
            if page > 10:  # 1000 repos is plenty for v1; same cap as _fetch
                log.warning(
                    "resolve_repo_pagination_cap",
                    extra={"install_id": install_id, "repo_id": repo_id},
                )
                return None
