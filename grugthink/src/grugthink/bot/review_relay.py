"""Fetch grug's real Elder review verdict for a PR and relay it into Discord.

Grug's own code-review engine (this repo's services/webhook/) is what
actually reviews PRs - the "Grug - Elder" check-run, produced by the
normal webhook flow whenever a PR is opened or updated. This module never
re-implements review logic or re-reviews anything itself; it reads the
real check-run GitHub already has and relays it in Grug's voice.

Read-only, so lower-stakes than task_relay's write-capable Hermes relay,
but still gated behind task_relay.is_authorized: Elder's findings on a
private repo can be real information a random Discord guild member
shouldn't get just by asking Grug, same trust boundary either way.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

import discord
import httpx

from ..logging_config import get_logger
from .task_relay import is_authorized

log = get_logger(__name__)

# Mirrors services/_shared/personas/tribe.py's CHECK_ELDER + its legacy
# aliases (CHECK_ELDER, LEGACY_CHECK_ELDER, LEGACY_CHECK_ELDER_EM,
# LEGACY_CHECK_ELDER_EM_SHORT). grugthink is a separate deployment from
# grug's webhook/api services and can't import that module directly, so
# the names are duplicated here - keep in sync if tribe.py's names change.
CHECK_ELDER_NAMES = (
    "Grug - Elder",
    "Grug - Code Review",
    "Grug — Code Review",
    "Grug — Elder",
)

# Every repo in task_relay.REPO_CHANNELS lives under this GitHub org.
GITHUB_ORG = "quadseven"

GITHUB_API = "https://api.github.com"

# Read-only "checks:read" token, deliberately separate from Hermes'
# broader GH_TOKEN - this module only ever reads check-run output, never
# writes anything. See k8s/DEPLOY.md for how it's provisioned.
_TOKEN_ENV_VAR = "GRUGTHINK_GITHUB_CHECKS_TOKEN"

_PR_REF_PATTERN = re.compile(r"#(\d+)\b")

# Elder's summary is a full findings table meant for GitHub's markdown
# renderer - trimmed hard so a Discord relay stays a relay, not a wall of
# text. Full detail always stays reachable via html_url.
_SUMMARY_MAX_CHARS = 500


@dataclass
class ElderVerdict:
    """One check-run's worth of Elder output, as reported by GitHub."""

    conclusion: Optional[str]  # "success" | "failure" | "neutral" | "action_required" | None if still running
    title: Optional[str]
    summary: Optional[str]
    html_url: Optional[str]


def extract_pr_number(clean_content: str) -> Optional[int]:
    """Find a `#123`-style PR reference in the request."""
    match = _PR_REF_PATTERN.search(clean_content)
    return int(match.group(1)) if match else None


def _get_token() -> Optional[str]:
    return os.environ.get(_TOKEN_ENV_VAR) or None


async def fetch_elder_verdict(repo: str, pr_number: int) -> Optional[ElderVerdict]:
    """Look up the Grug - Elder check-run for a PR's current head commit.

    Returns None if there's no token configured, the PR/check-run can't be
    found, or the request fails. Callers must treat None as "can't answer
    right now" - never as "Elder found nothing to say".
    """
    token = _get_token()
    if not token:
        log.warning("review_relay: no GitHub token configured (%s unset)", _TOKEN_ENV_VAR)
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(base_url=GITHUB_API, headers=headers, timeout=15.0) as client:
        try:
            pr_resp = await client.get(f"/repos/{GITHUB_ORG}/{repo}/pulls/{pr_number}")
            pr_resp.raise_for_status()
            head_sha = pr_resp.json()["head"]["sha"]

            checks_resp = await client.get(f"/repos/{GITHUB_ORG}/{repo}/commits/{head_sha}/check-runs")
            checks_resp.raise_for_status()
        except httpx.HTTPError:
            log.exception("review_relay: GitHub API call failed", extra={"repo": repo, "pr_number": pr_number})
            return None

    for run in checks_resp.json().get("check_runs", []):
        if run.get("name") in CHECK_ELDER_NAMES:
            output = run.get("output") or {}
            return ElderVerdict(
                conclusion=run.get("conclusion"),
                title=output.get("title"),
                summary=output.get("summary"),
                html_url=run.get("html_url"),
            )

    return None


_VERDICT_LINES = {
    "success": "Elder say good hunt!",
    "failure": "Elder find bad omen in hunt.",
    "neutral": "Elder look but no strong word either way.",
    "action_required": "Elder say tribe must act before hunt done.",
}


def format_verdict(verdict: Optional[ElderVerdict], bot_name: str, repo: str, pr_number: int) -> str:
    """Render an ElderVerdict as a Grug-voiced Discord message."""
    if verdict is None:
        return (
            f"{bot_name} look for Elder word on {repo} #{pr_number} but find nothing yet. "
            f"Maybe Elder still think, or maybe no such hunt."
        )

    if verdict.conclusion is None:
        return f"{bot_name} see Elder still look at {repo} #{pr_number}. Ask {bot_name} again soon."

    verdict_line = _VERDICT_LINES.get(verdict.conclusion, f"Elder verdict: {verdict.conclusion}.")

    parts = [f"{bot_name} bring word from Elder on {repo} #{pr_number}: {verdict_line}"]
    if verdict.title:
        parts.append(verdict.title)
    if verdict.summary:
        trimmed = verdict.summary.strip()
        if len(trimmed) > _SUMMARY_MAX_CHARS:
            trimmed = trimmed[:_SUMMARY_MAX_CHARS].rstrip() + "..."
        parts.append(trimmed)
    if verdict.html_url:
        parts.append(f"Full markings: {verdict.html_url}")

    return "\n\n".join(parts)


async def relay_review(
    original_message: discord.Message,
    bot_name: str,
    repo: str,
    pr_number: int,
) -> None:
    """Fetch and relay the current Elder verdict for repo #pr_number.

    Intended to be launched via ``asyncio.create_task`` from ``on_message``
    alongside ``task_relay.relay_to_hermes`` - a single network round trip,
    but still kept off the event handler's own await chain for consistency.
    """
    if not is_authorized(original_message.author.id):
        log.warning(
            "review_relay: relay attempt from unauthorized user",
            extra={"user_id": original_message.author.id, "user_name": str(original_message.author)},
        )
        await original_message.channel.send(f"{bot_name} no know you well enough for that. Ask Evan to add you first.")
        return

    verdict = await fetch_elder_verdict(repo, pr_number)
    await original_message.channel.send(format_verdict(verdict, bot_name, repo, pr_number))
