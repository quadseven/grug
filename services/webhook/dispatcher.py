"""Webhook → persona dispatch.

Routes GitHub events to active personas. v1 only handles
pull_request → TPM persona. Future personas register here.

Skips silently if installation not allowlisted (defense-in-depth — full
allowlist gate logic lands in Slice 5 #26 with admin-flippable DDB flag).
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("grug.webhook.dispatcher")


def dispatch(event_name: str, payload: dict[str, Any]) -> dict[str, str]:
    """Route a webhook event to its persona handlers. Returns audit dict."""
    if event_name == "pull_request":
        return _handle_pull_request(payload)
    if event_name == "pull_request_review":
        # v1.5+ — code-reviewer persona consumes this
        return {"status": "no_op", "reason": "no persona for pull_request_review yet"}
    return {"status": "no_op", "reason": f"no handler for event {event_name}"}


def _handle_pull_request(payload: dict[str, Any]) -> dict[str, str]:
    action = payload.get("action", "")
    # Only fire on actions that change the gate-relevant state
    if action not in {"opened", "edited", "synchronize", "ready_for_review", "reopened"}:
        return {"status": "no_op", "reason": f"pull_request action={action} not gated"}

    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    head = pr.get("head") or {}

    installation_id = installation.get("id")
    owner = (repo.get("owner") or {}).get("login") or repo.get("full_name", "").split("/")[0]
    repo_name = repo.get("name")
    head_sha = head.get("sha")
    pr_body = pr.get("body") or ""
    pr_number = pr.get("number")

    if not all([installation_id, owner, repo_name, head_sha, pr_number]):
        log.warning(
            "pull_request_payload_incomplete",
            extra={
                "installation_id": installation_id, "owner": owner,
                "repo_name": repo_name, "head_sha": head_sha,
                "pr_number": pr_number,
            },
        )
        return {"status": "skip", "reason": "incomplete_payload"}

    # Lazy import — keeps cold-start cheap when only non-PR events fire
    from personas.tpm.persona import evaluate_pull_request  # type: ignore

    result = evaluate_pull_request(
        installation_id=int(installation_id),
        owner=owner,
        repo=repo_name,
        head_sha=head_sha,
        pr_body=pr_body,
        pr_number=int(pr_number),
    )
    return {
        "status": "dispatched",
        "persona": "tpm",
        "result": "pass" if result.passed else "fail",
    }
