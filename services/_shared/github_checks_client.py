"""GitHub Checks API client — post + update check-runs.

Wraps the two endpoints we need for TPM persona's PR-gate. Tokens
fetched per-installation via github_app_auth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

import httpx

log = logging.getLogger("grug.checks_client")

_GH_API = "https://api.github.com"

CheckConclusion = Literal[
    "success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required"
]


@dataclass(frozen=True)
class CheckRunResult:
    name: str
    head_sha: str
    status: Literal["queued", "in_progress", "completed"]
    conclusion: CheckConclusion | None
    title: str
    summary: str
    text: str | None = None

    def __post_init__(self) -> None:
        # type-design-analyzer: enforce GitHub's cross-field invariant
        # "status=='completed' iff conclusion is set". Earlier code
        # allowed CheckRunResult(status='queued', conclusion='success')
        # which GitHub 422s — fail at construction instead.
        is_terminal = self.status == "completed"
        has_conclusion = self.conclusion is not None
        if is_terminal != has_conclusion:
            raise ValueError(
                "CheckRunResult: status=='completed' iff conclusion is "
                f"not None (got status={self.status!r}, "
                f"conclusion={self.conclusion!r})"
            )


def github_error_diagnostics(exc: BaseException) -> dict[str, object]:
    """Why GitHub refused a request, for the failure log line.

    Pulls only GitHub's own error `message` and `documentation_url` from the
    response body plus the `X-GitHub-Request-Id` header (what GitHub support
    asks for). Never reads the REQUEST (body or Authorization header), so no
    token or PR content can reach a log. Returns {} for anything that is not
    an HTTPStatusError, and never raises: diagnostics must not mask the
    failure they describe (a 403 with an unexplained body is how the
    2026-10-05 check-run outage stayed a mystery)."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return {}
    out: dict[str, object] = {}
    try:
        resp = exc.response
        out["gh_request_id"] = resp.headers.get("x-github-request-id")
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            out["gh_message"] = str(body.get("message") or "")[:300]
            out["gh_documentation_url"] = str(body.get("documentation_url") or "")[:300]
    except Exception as e:  # noqa: BLE001 - diagnostics are best-effort by contract
        log.debug("github_error_diagnostics_failed", extra={"kind": type(e).__name__})
    return out


# GitHub documented limit is 65535; leave headroom for the marker.
_MAX_SUMMARY_CHARS = 65000


def post_check_run(
    install_token: str,
    owner: str,
    repo: str,
    result: CheckRunResult,
    external_id: str | None = None,
) -> dict:
    """POST a check-run.

    NOT idempotent on (name, head_sha) - this docstring claimed otherwise
    from the original slice-4 implementation until grug#947 corrected it.
    GitHub's create endpoint always makes a NEW check-run object; two
    identical POSTs produce two separate runs, both visible on the PR.
    `services/webhook/delivery_replay.py` (#407) independently verified
    this live and depends on it explicitly for its own correctness -
    callers that need to avoid a duplicate must check for an existing
    run themselves (list check-runs for the head SHA, match by name)
    before calling this, never assume the API dedupes for them."""
    # GitHub 422s output.summary over 65535 chars, and a 422 here vanishes
    # the ENTIRE check-run from the PR (#553 audit). The findings table is
    # message-count-bounded but not finding-count-bounded, so enforce the
    # cap at this ONE choke point, visibly - a truncated summary beats an
    # absent check-run.
    summary = result.summary
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS] + "\n\n(summary truncated)"
    body = {
        "name": result.name,
        "head_sha": result.head_sha,
        "status": result.status,
        "output": {
            "title": result.title,
            "summary": summary,
            **({"text": result.text} if result.text else {}),
        },
    }
    if result.conclusion:
        body["conclusion"] = result.conclusion
    if external_id:
        body["external_id"] = external_id

    resp = httpx.post(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/check-runs",
        json=body,
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        log.warning(
            "check_run_post_http_error",
            extra={
                "repo": f"{owner}/{repo}",
                "head_sha": result.head_sha[:8],
                "check_name": result.name,
                "status_code": resp.status_code,
                **github_error_diagnostics(e),
            },
        )
        raise
    return resp.json()


def _headers(install_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {install_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def list_check_runs_for_ref(
    install_token: str, owner: str, repo: str, ref: str,
) -> list[dict]:
    """List check-runs for a commit/ref, `filter=latest` (one entry per
    name - a re-run of the same check replaces its predecessor in this
    view, matching what the PR page shows). Paginated: a busy commit can
    carry more than one page of distinct check names even under
    `filter=latest` (grug#947, same shape `rerun.py`'s Elder-specific
    completion guard already established live). `_MAX_PAGES` is a
    runaway backstop, not a real ceiling - no real commit needs it."""
    _MAX_PAGES = 10
    runs: list[dict] = []
    seen = 0
    for page in range(1, _MAX_PAGES + 1):
        resp = httpx.get(
            f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/commits/{quote(ref, safe='')}/check-runs",
            params={"filter": "latest", "per_page": 100, "page": page},
            headers=_headers(install_token),
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json() or {}
        total = int(body.get("total_count") or 0)
        batch = body.get("check_runs") or []
        runs.extend(batch)
        seen += len(batch)
        if not batch or seen >= total:
            break
    return runs


def patch_check_run(
    install_token: str,
    owner: str,
    repo: str,
    check_run_id: int,
    result: CheckRunResult,
) -> dict:
    """PATCH an EXISTING check-run by id to a new status/conclusion.

    Unlike `post_check_run` (create), this genuinely is idempotent in the
    sense that matters here: it updates the SAME object rather than
    creating a new one, so grug#947's sweeper can safely re-patch a run
    it already closed out without multiplying check-runs."""
    summary = result.summary
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS] + "\n\n(summary truncated)"
    body = {
        "name": result.name,
        "status": result.status,
        "output": {
            "title": result.title,
            "summary": summary,
            **({"text": result.text} if result.text else {}),
        },
    }
    if result.conclusion:
        body["conclusion"] = result.conclusion
    resp = httpx.patch(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/check-runs/{int(check_run_id)}",
        json=body,
        headers=_headers(install_token),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()
