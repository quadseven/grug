"""Webhook → persona dispatch.

Routes GitHub events to active personas. v1 handles:
  - installation / installation_repositories: record + delete INST# rows
  - pull_request: TPM persona (gated on installer allowlist)
  - pull_request_review: placeholder for v1.5 code-reviewer persona

Allowlist gate (Slice 5 #26): non-allowlisted installs no_op silently
BEFORE persona work runs. Defense-in-depth — App registration is
public but webhook never acts on PRs from non-allowlisted users.
"""

from __future__ import annotations

import logging
from typing import Any

from adapters.install_store import (
    delete_installation,
    is_install_allowlisted,
    is_persona_enabled,
    record_installation,
)

log = logging.getLogger("grug.webhook.dispatcher")


def dispatch(event_name: str, payload: dict[str, Any]) -> dict[str, str]:
    """Route a webhook event to its persona handlers. Returns audit dict."""
    if event_name == "installation":
        return _handle_installation(payload)
    if event_name == "installation_repositories":
        # Repo-list change on an existing install — install row already
        # exists; nothing to record (per-repo config lands in Slice 7+).
        return {"status": "no_op", "reason": "installation_repositories acknowledged"}
    if event_name == "pull_request":
        return _handle_pull_request(payload)
    if event_name == "pull_request_review":
        return {"status": "no_op", "reason": "no persona for pull_request_review yet"}
    return {"status": "no_op", "reason": f"no handler for event {event_name}"}


def _handle_installation(payload: dict[str, Any]) -> dict[str, str]:
    action = payload.get("action", "")
    install = payload.get("installation") or {}
    install_id = install.get("id")
    if not install_id:
        return {"status": "skip", "reason": "no installation.id"}

    if action == "deleted":
        delete_installation(int(install_id))
        return {"status": "recorded", "action": "deleted"}

    if action in {"created", "new_permissions_accepted", "unsuspend"}:
        account = install.get("account") or {}
        sender = payload.get("sender") or {}
        installed_by = sender.get("id") or account.get("id")
        if not installed_by:
            return {"status": "skip", "reason": "no sender.id or account.id"}
        record_installation(
            install_id=int(install_id),
            account_login=account.get("login", ""),
            account_type=account.get("type", "User"),
            installed_by_user_id=int(installed_by),
        )
        return {"status": "recorded", "action": action}

    return {"status": "no_op", "reason": f"installation action={action} unhandled"}


def _handle_pull_request(payload: dict[str, Any]) -> dict[str, str]:
    action = payload.get("action", "")
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

    # Allowlist gate (Slice 5 #26). Defense-in-depth — non-allowlisted
    # installs no_op silently. Avoids any GitHub API calls (no install
    # token request, no check-run post) so we don't leak Grug presence
    # to repos whose installer hasn't been admin-approved.
    if not is_install_allowlisted(int(installation_id)):
        log.info(
            "allowlist_gate_skip",
            extra={
                "installation_id": installation_id,
                "owner": owner, "repo": repo_name, "pr_number": pr_number,
            },
        )
        return {"status": "no_op", "reason": "installer not allowlisted"}

    # Per-repo persona toggle (Slice 7 #28). Lets users disable Grug on
    # noisy repos without uninstalling the App entirely. Defaults to
    # enabled — explicit opt-out per repo via dashboard.
    repo_id = repo.get("id")
    if repo_id is not None and not is_persona_enabled(
        int(installation_id), int(repo_id), "tpm",
    ):
        log.info(
            "persona_disabled_skip",
            extra={
                "installation_id": installation_id,
                "owner": owner, "repo": repo_name, "pr_number": pr_number,
                "persona": "tpm",
            },
        )
        return {"status": "no_op", "reason": "tpm disabled for this repo"}

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
