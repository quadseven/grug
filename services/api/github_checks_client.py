"""GitHub Checks API client — post + update check-runs.

Wraps the two endpoints we need for TPM persona's PR-gate. Tokens
fetched per-installation via github_app_auth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

_GH_API = "https://api.github.com"

CheckConclusion = Literal[
    "success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required"
]


@dataclass
class CheckRunResult:
    name: str
    head_sha: str
    status: Literal["queued", "in_progress", "completed"]
    conclusion: CheckConclusion | None
    title: str
    summary: str
    text: str | None = None


def post_check_run(
    install_token: str,
    owner: str,
    repo: str,
    result: CheckRunResult,
    external_id: str | None = None,
) -> dict:
    """POST a check-run. Idempotent on (name, head_sha) per GitHub spec."""
    body = {
        "name": result.name,
        "head_sha": result.head_sha,
        "status": result.status,
        "output": {
            "title": result.title,
            "summary": result.summary,
            **({"text": result.text} if result.text else {}),
        },
    }
    if result.conclusion:
        body["conclusion"] = result.conclusion
    if external_id:
        body["external_id"] = external_id

    resp = httpx.post(
        f"{_GH_API}/repos/{owner}/{repo}/check-runs",
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
