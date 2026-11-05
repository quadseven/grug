"""Elder review of a pushed branch before a pull request exists (grug#1438).

  POST /api/v1/pre-pr-review  {repo, base, head}
       -> {findings: [{file, line, severity, class, text}], ...}

A lane asks for the Elder review of `base...head` on a repo its user's
installation covers, fixes what comes back, and opens a cleaner pull
request. The route writes nothing to GitHub: every GitHub call is a GET,
made with a token minted down to `contents:read` on the one repo, so a
write would fail at GitHub even if a later change tried one.

Order matters for the refusals:
  1. the repo must sit under one of the caller's installations (404),
  2. the per-installation cap must admit the call (429; nothing else is
     read or written past this point),
  3. the refs must resolve (404).
Then the compare diff is read, `.grug.yaml` is read at the BASE commit
(a branch cannot loosen its own review, the same rule Elder applies to a
pull request), Elder reviews the kept hunks, and one activity row of kind
`pre_pr_review` is recorded.

The review is Elder's core: parse_diff, the `.grug.yaml` ignore split and
path instructions, assemble_evidence, review_diff, evaluate_diff. Steps
that need a pull request (linked issues, CI status, prior threads, the
judge's publish filters) have nothing to read here and are not run.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from adapters.install_store import list_user_installations, put_check_verdict
from adapters.pg_rate_limit_store import try_reserve_slot
from adapters.user_store import UserIdentity
from auth.dependencies import require_authenticated
from github_app_auth import get_scoped_install_token
from installations import _repo_belongs_to_install, _repo_owned_by_install
from llm_client import Hunk, LlmReviewResponse, PrContext, review_diff
from personas.code_reviewer.diff_parser import DiffHunk, parse_diff
from personas.code_reviewer.persona import CodeReviewEvaluation, evaluate_diff
from personas.code_reviewer.repo_config import (
    CONFIG_PATH,
    parse_repo_config,
    split_ignored_hunks,
    with_instructions,
)
from personas.review_publish import fetch_file_contents
from review_evidence import assemble_evidence

log = logging.getLogger("grug.api.pre_pr_review")

router = APIRouter(prefix="/api/v1")

ACTIVITY_KIND = "pre_pr_review"
_GH_API = "https://api.github.com"
_TIMEOUT = 30.0
_READ_ONLY = {"contents": "read", "metadata": "read"}

# Per-installation cap. Each call runs a full LLM review, so the default is
# deliberately low; an operator raises it through the env, not code.
_MINUTE_LIMIT = int(os.getenv("GRUG_PRE_PR_REVIEW_PER_MINUTE", "5"))
_DAY_LIMIT = int(os.getenv("GRUG_PRE_PR_REVIEW_PER_DAY", "100"))

_REF_CHARS = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")


class PrePrReviewRequest(BaseModel):
    """`repo` is "owner/name"; `base` and `head` are pushed refs (branch,
    tag or SHA). Refs are checked here so nothing malformed reaches a
    GitHub URL."""

    model_config = ConfigDict(extra="forbid")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base: str
    head: str

    @field_validator("base", "head")
    @classmethod
    def _valid_ref(cls, ref: str) -> str:
        if (
            not _REF_CHARS.match(ref)
            or ".." in ref
            or ref.startswith(("-", "/"))
            or ref.endswith(("/", ".lock"))
        ):
            raise ValueError("not a valid git ref")
        return ref


def _installation_for(user: UserIdentity, repo: str) -> int:
    """The caller's installation that covers `repo`, or 404. Owner match
    first (no GitHub call); then each installation's visible repo list, so a
    renamed account still resolves (the same rule as the dashboard routes).
    A repo outside every installation is 404, never 403, so the route does
    not confirm which repos exist."""
    installs: list[tuple[int, dict[str, Any]]] = []
    for item in list_user_installations(user.github_user_id):
        try:
            installs.append((int(str(item.get("PK", "")).split("#", 1)[1]), item))
        except (IndexError, ValueError):
            log.error("pre_pr_review_corrupt_install_pk", extra={"pk": item.get("PK")})
    for install_id, install in installs:
        if _repo_owned_by_install(install, repo):
            return install_id
    for install_id, install in installs:
        if _repo_belongs_to_install(install_id, install, repo):
            return install_id
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="repo not found")


def _get(token: str, path: str, accept: str) -> httpx.Response:
    """The only GitHub call shape this module makes: a GET."""
    resp = httpx.get(
        f"{_GH_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp


def _upstream_error(e: Exception, what: str) -> HTTPException:
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (404, 422):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")
    log.warning(
        "pre_pr_review_github_failed",
        extra={"what": what, "error": f"{type(e).__name__}: {e}"},
    )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="github upstream error")


def _read_branch(
    token: str, owner: str, name: str, base: str, head: str,
) -> tuple[str, str, str]:
    """(base_sha, head_sha, unified diff) for `base...head`."""
    repo_path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    try:
        base_sha = _get(
            token, f"{repo_path}/commits/{quote(base, safe='/')}",
            "application/vnd.github.sha",
        ).text.strip()
        head_sha = _get(
            token, f"{repo_path}/commits/{quote(head, safe='/')}",
            "application/vnd.github.sha",
        ).text.strip()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        raise _upstream_error(e, "ref") from None
    try:
        diff = _get(
            token, f"{repo_path}/compare/{base_sha}...{head_sha}",
            "application/vnd.github.diff",
        ).text
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        raise _upstream_error(e, "compare") from None
    return base_sha, head_sha, diff


def _review(
    *, install_id: int, repo: str, token: str, base_sha: str, head_sha: str,
    diff: str,
) -> tuple[CodeReviewEvaluation, tuple[str, ...]]:
    """Elder's core review of one diff. Returns the evaluation and the paths
    `.grug.yaml` ignored."""
    owner, name = repo.split("/", 1)
    try:
        fetched = fetch_file_contents(
            token, owner, name, (CONFIG_PATH,), base_sha, only_404_skips=True,
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        raise _upstream_error(e, "config") from None
    config = parse_repo_config(fetched.get(CONFIG_PATH))

    hunks: tuple[DiffHunk, ...]
    hunks, ignored = split_ignored_hunks(parse_diff(diff), config)
    changed = tuple(dict.fromkeys(h.file_path for h in hunks))
    file_contents = fetch_file_contents(token, owner, name, changed, head_sha)
    pr_context: PrContext = {
        "installation_id": install_id,
        "repo": repo,
        "pr_number": 0,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "pr_base_sha": base_sha,
    }
    evidence = assemble_evidence(
        [Hunk(path=h.file_path, body=h.body) for h in hunks],
        pr_context=pr_context,
        file_contents=file_contents,
        repo_docs_context=with_instructions(None, changed, config),
    )
    response: LlmReviewResponse = review_diff(evidence, installation_id=install_id)
    if response.kind != "reviewed":
        log.warning(
            "pre_pr_review_llm_degraded",
            extra={"installation_id": install_id, "repo": repo, "kind": response.kind},
        )
    return evaluate_diff(hunks, response), ignored


def _record_activity(
    *, install_id: int, repo: str, base_sha: str, head_sha: str,
    evaluation: CodeReviewEvaluation,
) -> None:
    """Best-effort: the review already ran, so a store blip must not turn
    its answer into a 500."""
    count = len(evaluation.findings)
    try:
        put_check_verdict(
            install_id=install_id,
            persona=ACTIVITY_KIND,
            repo=repo,
            pr_number=0,
            head_sha=head_sha,
            conclusion=evaluation.conclusion,
            summary=f"Pre-PR review of {head_sha[:7]} against {base_sha[:7]}: {count} finding(s)",
            findings_count=count,
            blocking=False,
            created_at=datetime.now(timezone.utc).isoformat(),
            degraded_reason=evaluation.degraded_reason,
        )
    except Exception as e:  # noqa: BLE001 - telemetry write after the review
        log.warning(
            "pre_pr_review_activity_failed",
            extra={"installation_id": install_id, "repo": repo,
                   "error": f"{type(e).__name__}: {e}"},
        )


@router.post("/pre-pr-review")
def pre_pr_review(
    body: PrePrReviewRequest,
    user: Annotated[UserIdentity, Depends(require_authenticated)],
) -> dict[str, Any]:
    """Run the Elder review on `base...head` and return its findings. Writes
    nothing to GitHub. 404 for a repo outside the caller's installations or
    an unknown ref; 429 past the per-installation cap."""
    install_id = _installation_for(user, body.repo)

    slot = try_reserve_slot(
        name=f"{ACTIVITY_KIND}:{install_id}",
        minute_limit=_MINUTE_LIMIT,
        day_limit=_DAY_LIMIT,
    )
    if not slot.admitted:
        log.info(
            "pre_pr_review_rate_limited",
            extra={"installation_id": install_id, "minute_count": slot.minute_count,
                   "day_count": slot.day_count},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="pre-PR review limit reached for this installation",
        )

    owner, name = body.repo.split("/", 1)
    try:
        token = get_scoped_install_token(
            install_id, repositories=[name], permissions=dict(_READ_ONLY),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        raise _upstream_error(e, "repo") from None

    base_sha, head_sha, diff = _read_branch(token, owner, name, body.base, body.head)
    evaluation, ignored = _review(
        install_id=install_id, repo=body.repo, token=token,
        base_sha=base_sha, head_sha=head_sha, diff=diff,
    )
    _record_activity(
        install_id=install_id, repo=body.repo, base_sha=base_sha,
        head_sha=head_sha, evaluation=evaluation,
    )
    return {
        "repo": body.repo,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "conclusion": evaluation.conclusion,
        "degraded_reason": evaluation.degraded_reason,
        "ignored_paths": list(ignored),
        "findings": [
            {
                "file": f.file,
                "line": f.line,
                "severity": f.severity,
                "class": f.rule_name,
                "text": f.message,
            }
            for f in evaluation.findings
        ],
    }
