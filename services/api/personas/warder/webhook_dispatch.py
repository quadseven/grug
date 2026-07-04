# MIRRORED — sibling at services/webhook/personas/warder/webhook_dispatch.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Warder persona - webhook pull_request dispatch (#471, ADR-0010).

Registered with `actions=("closed",)` - the first persona off the
PR-update action set. The MERGED + default-branch checks live here (the
registry's actions filter is per-action, not per-flag): an unmerged
close or a merge to a side branch returns `skipped` without any GitHub
call. Runs INLINE (two GitHub API calls, no LLM) - escalate to async if
DD shows ACK-path latency.
"""

from __future__ import annotations

import logging
import os

from personas.registry import PullRequestContext

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.warder.webhook_dispatch")


def dispatch_pull_request(ctx: PullRequestContext) -> dict[str, str]:
    pr = ctx.payload.get("pull_request") or {}
    if not pr.get("merged"):
        # A closed-without-merge PR has no release story.
        return {"persona": "warder", "result": "skipped"}
    base_ref = ((pr.get("base") or {}).get("ref")) or ""
    default_branch = ((ctx.payload.get("repository") or {}).get("default_branch")) or "main"
    if base_ref != default_branch:
        # Merges to side branches are not release events.
        return {"persona": "warder", "result": "skipped"}

    # The merge commit (not the PR head) is what lands on the default
    # branch - anchor the check-run there when present.
    sha = pr.get("merge_commit_sha") or ctx.head_sha

    from personas.warder.dispatch import dispatch_warder_release  # lazy: cold-start

    return dispatch_warder_release(
        installation_id=ctx.installation_id,
        owner=ctx.owner,
        repo_name=ctx.repo_name,
        head_sha=sha,
        pr_number=ctx.pr_number,
    )
