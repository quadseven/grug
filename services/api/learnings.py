"""Cave Drawings (#1203): the operator API over Grug's own learnings.

Learnings are the rules Grug mined from maintainers' replies (ADR-0020).
They steer every later review, so an operator must be able to see and
correct them. All endpoints are admin-only (`require_admin`):

  GET    /api/v1/admin/learnings?repo=&q=&status=   list + search
  GET    /api/v1/admin/learnings/export?repo=        portable document
  POST   /api/v1/admin/learnings/import              dry run by default
  POST   /api/v1/admin/learnings/preview             files a scope matches
  GET    /api/v1/admin/learnings/{owner}/{name}/{id} inspect (with history)
  PATCH  /api/v1/admin/learnings/{owner}/{name}/{id} edit text/scope/links
  POST   /api/v1/admin/learnings/{owner}/{name}/{id}/approve
  POST   /api/v1/admin/learnings/{owner}/{name}/{id}/disable
  DELETE /api/v1/admin/learnings/{owner}/{name}/{id}

Decisions (matching, lifecycle, import planning) live in the pure
`cave_drawings` module; persistence lives in the install store. This
module only maps HTTP onto them. Every mutation logs a structured audit
event naming the actor, the same posture as admin.py.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from adapters import install_store
from adapters.user_store import UserIdentity
from auth.dependencies import require_admin
from cave_drawings import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUSES,
    effective_status,
    export_document,
    is_valid_repo,
    matches_query,
    plan_import,
    preview_paths,
    validate_scope_path,
)
from github_app_auth import with_install_token_retry

log = logging.getLogger("grug.api.learnings")

router = APIRouter(prefix="/api/v1/admin/learnings")

_GH_API = "https://api.github.com"
_ID_PATTERN = r"^[0-9a-f]{12}$"
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class LearningPatch(BaseModel):
    """Edit payload. Omitted fields are left alone. A new `text` is a new
    rule: it supersedes this one and keeps the version history."""
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, max_length=1000)
    scope_path: str | None = Field(default=None, max_length=200)
    conflicts_with: list[str] | None = Field(default=None, max_length=50)


class PreviewRequest(BaseModel):
    """Either an explicit file list, or a repo + install to read the tree
    of `ref` from GitHub."""
    model_config = ConfigDict(extra="forbid")
    scope_path: str = Field(max_length=200)
    paths: list[str] | None = Field(default=None, max_length=100_000)
    repo: str | None = None
    install_id: int | None = None
    ref: str = Field(default="HEAD", max_length=200)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: dict[str, Any]
    target_repo: str | None = None
    # Dry run is the DEFAULT: an import writes only when asked to, and even
    # then every row lands pending until an operator approves it.
    dry_run: bool = True


def _repo_or_400(repo: str) -> str:
    if not is_valid_repo(repo):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="repo must be owner/name")
    return repo


def _id_or_404(learning_id: str) -> str:
    if not re.match(_ID_PATTERN, learning_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="learning not found")
    return learning_id


# String fields copied into the view as-is ("" when absent).
_VIEW_STR_FIELDS = (
    "id", "text", "created_at", "last_used_at", "supersedes", "superseded_by",
    "approved_by", "approved_at",
)


def _text(row: dict[str, Any], key: str) -> str:
    return str(row.get(key) or "")


def _source(row: dict[str, Any], repo: str) -> dict[str, Any]:
    """Provenance: the PR and review thread the learning was taught in."""
    pr = int(row.get("source_pr") or 0)
    comment_id = int(row.get("source_comment_id") or 0)
    thread_url = f"https://github.com/{repo}/pull/{pr}" if repo and pr else ""
    if thread_url and comment_id:
        thread_url += f"#discussion_r{comment_id}"
    return {
        "pr": pr, "comment_id": comment_id,
        "author": _text(row, "author"), "thread_url": thread_url,
    }


def _updated_at(row: dict[str, Any], history: list[dict[str, Any]]) -> str:
    """The last lifecycle change, else the last re-teach, else creation."""
    last_change = str(history[-1].get("at") or "") if history else ""
    return last_change or _text(row, "reinforced_at") or _text(row, "created_at")


def _view(row: dict[str, Any]) -> dict[str, Any]:
    """The operator's view of one learning: scope, the deterministic
    matcher, provenance, lifecycle, usage, and history."""
    repo = _text(row, "repo")
    history = list(row.get("history") or [])
    scope_path = _text(row, "scope_path")
    return {
        **{key: _text(row, key) for key in _VIEW_STR_FIELDS},
        "repo": repo,
        "status": effective_status(row),
        "scope": {"kind": "repo", "repo": repo, "org": repo.split("/", 1)[0]},
        "scope_path": scope_path,
        "matcher": {"glob": scope_path, "repo_wide": not scope_path, "engine": "fnmatchcase"},
        "source": _source(row, repo),
        "updated_at": _updated_at(row, history),
        "usage_count": int(row.get("usage_count") or 0),
        "version": int(row.get("version") or 1),
        "conflicts_with": list(row.get("conflicts_with") or []),
        "history": history,
    }


def _audit(event: str, user: UserIdentity, repo: str, learning_id: str, **extra: Any) -> None:
    log.info(event, extra={
        "actor_login": user.login, "actor_id": user.github_user_id,
        "repo": repo, "learning_id": learning_id, **extra,
    })


@router.get("")
def list_learnings(
    repo: str = "",
    q: str = "",
    status: str = "",  # noqa: A002 - query parameter name the SPA sends
    user: UserIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """Every learning (or one repo's), filtered by a text search and a
    lifecycle status. Includes pending, disabled and superseded rows."""
    if status and status not in STATUSES:
        raise HTTPException(
            400, detail=f"status must be one of {list(STATUSES)}",
        )
    repos = [_repo_or_400(repo)] if repo else install_store.list_learning_repos()
    out: list[dict[str, Any]] = []
    for r in repos:
        for row in install_store.list_learnings(r):
            row_d = dict(row)
            if status and effective_status(row_d) != status:
                continue
            if not matches_query(row_d, q):
                continue
            out.append(_view(row_d))
    return {"learnings": out}


@router.get("/export")
def export_learnings(
    repo: str,
    user: UserIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """A portable document of one repo's learnings (any status) that
    another Grug instance can import."""
    rows = [dict(r) for r in install_store.list_learnings(_repo_or_400(repo))]
    _audit("learnings_exported", user, repo, "", count=len(rows))
    return export_document(rows)


@router.post("/import")
def import_learnings(
    body: ImportRequest,
    user: UserIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """Validate and deduplicate a document (always), then, only when
    `dry_run` is false and the plan is clean, apply it in one transaction.
    Imported rows are pending: approving each one is the activation step."""
    target = body.target_repo
    if target:
        _repo_or_400(target)
    # Resolve the repository first so duplicates are checked against it.
    probe = plan_import(body.document, target_repo=target, existing_ids=set())
    existing: set[str] = set()
    if probe.repo:
        existing = {str(r.get("id", "")) for r in install_store.list_learnings(probe.repo)}
    plan = plan_import(body.document, target_repo=target, existing_ids=existing)
    result = {**plan.as_dict(), "applied": False, "created": []}
    if body.dry_run:
        return result
    if not plan.ok:
        raise HTTPException(422, detail=result)
    try:
        created = install_store.import_learnings(plan.repo, plan.to_create, actor=user.login)
    except install_store.LearningImportConflict:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="a learning in this import was created since the dry run; "
                   "nothing was imported - run the dry run again",
        ) from None
    _audit("learnings_imported", user, plan.repo, "", count=len(created))
    return {**result, "applied": True, "created": created}


def _fetch_repo_paths(install_id: int, repo: str, ref: str) -> list[str]:
    """Every file path in `repo` at `ref`, from GitHub's recursive tree."""
    def _fetch(token: str) -> list[str]:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"{_GH_API}/repos/{repo}/git/trees/{ref}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
                params={"recursive": "1"},
            )
            resp.raise_for_status()
            body = resp.json()
        if body.get("truncated"):
            # A partial list would make the preview lie about "exactly".
            raise HTTPException(
                422,
                detail="repository tree too large to list; pass paths explicitly",
            )
        return [e["path"] for e in body.get("tree", []) if e.get("type") == "blob"]

    return with_install_token_retry(install_id, _fetch)


@router.post("/preview")
def preview_learning_paths(
    body: PreviewRequest,
    user: UserIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """Exactly which files a scope applies to, before it is activated."""
    reason = validate_scope_path(body.scope_path)
    if reason:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=reason)
    if body.paths is not None:
        paths = body.paths
    elif body.repo and body.install_id:
        # The ref is interpolated into the GitHub API path: refuse anything
        # that could step out of the trees endpoint or add a query.
        if not _REF_RE.match(body.ref) or ".." in body.ref:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid ref")
        paths = _fetch_repo_paths(body.install_id, _repo_or_400(body.repo), body.ref)
    else:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="pass paths, or repo and install_id",
        )
    matches = preview_paths(body.scope_path, paths)
    return {
        "scope_path": body.scope_path,
        "repo_wide": not body.scope_path,
        "matches": matches,
        "match_count": len(matches),
        "total_files": len(set(paths)),
    }


@router.get("/{owner}/{name}/{learning_id}")
def get_learning(
    owner: str, name: str, learning_id: str,
    user: UserIdentity = Depends(require_admin),
) -> dict[str, Any]:
    repo = _repo_or_400(f"{owner}/{name}")
    row = install_store.get_learning(repo, _id_or_404(learning_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="learning not found")
    return _view(dict(row))


@router.patch("/{owner}/{name}/{learning_id}")
def patch_learning(
    owner: str, name: str, learning_id: str,
    body: LearningPatch,
    user: UserIdentity = Depends(require_admin),
) -> dict[str, Any]:
    repo = _repo_or_400(f"{owner}/{name}")
    try:
        row = install_store.edit_learning(
            repo, _id_or_404(learning_id), actor=user.login,
            text=body.text, scope_path=body.scope_path, conflicts_with=body.conflicts_with,
        )
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="learning not found") from None
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from None
    _audit("learning_edited", user, repo, learning_id, new_id=row.get("id", ""))
    return _view(dict(row))


def _set_status(owner: str, name: str, learning_id: str, new: str, user: UserIdentity) -> dict[str, Any]:
    repo = _repo_or_400(f"{owner}/{name}")
    try:
        row = install_store.set_learning_status(
            repo, _id_or_404(learning_id), new, actor=user.login,
        )
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="learning not found") from None
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from None
    _audit("learning_status_changed", user, repo, learning_id, status=new)
    return _view(dict(row))


@router.post("/{owner}/{name}/{learning_id}/approve")
def approve_learning(
    owner: str, name: str, learning_id: str,
    user: UserIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """Activate a pending (or re-enable a disabled) learning."""
    return _set_status(owner, name, learning_id, STATUS_ACTIVE, user)


@router.post("/{owner}/{name}/{learning_id}/disable")
def disable_learning(
    owner: str, name: str, learning_id: str,
    user: UserIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """Stop injecting a learning, keeping it and its history."""
    return _set_status(owner, name, learning_id, STATUS_DISABLED, user)


@router.delete("/{owner}/{name}/{learning_id}")
def delete_learning(
    owner: str, name: str, learning_id: str,
    user: UserIdentity = Depends(require_admin),
) -> dict[str, Any]:
    repo = _repo_or_400(f"{owner}/{name}")
    if not install_store.delete_learning(repo, _id_or_404(learning_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="learning not found")
    _audit("learning_deleted", user, repo, learning_id)
    return {"deleted": True, "id": learning_id}
