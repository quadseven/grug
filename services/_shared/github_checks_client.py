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


# GitHub documented limit is 65535; leave headroom for the marker.
_MAX_SUMMARY_CHARS = 65000


def post_check_run(
    install_token: str,
    owner: str,
    repo: str,
    result: CheckRunResult,
    external_id: str | None = None,
) -> dict:
    """POST a check-run. Idempotent on (name, head_sha) per GitHub spec."""
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
    resp.raise_for_status()
    return resp.json()
