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
    get_installation,
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
    if event_name == "issue_comment":
        return _handle_issue_comment(payload)
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

    if action == "created":
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

    if action in {"new_permissions_accepted", "unsuspend"}:
        # Codex post-review #51 — preserve the original installer.
        # Sender on these events can be a different org admin than the
        # one who originally clicked Install. Overwriting installed_by
        # would shift allowlist resolution to a user who never agreed
        # to be the install owner.
        existing = get_installation(int(install_id))
        if not existing:
            # Edge case: we missed the `created` event somehow. Fall
            # back to record using sender so allowlist gate has SOME
            # owner to resolve against.
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
            return {"status": "recorded", "action": f"{action}_backfill"}
        return {"status": "no_op", "reason": f"{action} ack — installer preserved"}

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


# Closes #2 — `/grug recheck` slash command. PR comments containing
# the trigger phrase re-fire DoR check without requiring an empty commit.
# Trigger surface is `issue_comment` event (PR comments arrive there too,
# distinguished by `issue.pull_request` truthy). Author-or-collaborator
# auth check stops drive-by spam.
import re as _re  # noqa: E402

_RECHECK_PAT = _re.compile(r"^\s*/grug\s+recheck\b", _re.IGNORECASE | _re.MULTILINE)
# Author-only roles that satisfy the auth gate. PR author is implicit
# (sender.login == pr.user.login). Collaborator perms come from
# `permission` field on `GET /repos/{o}/{r}/collaborators/{u}/permission`.
_AUTHORIZED_ROLES = {"admin", "maintain", "write"}


def _handle_issue_comment(payload: dict[str, Any]) -> dict[str, str]:
    action = payload.get("action", "")
    if action != "created":
        return {"status": "no_op", "reason": f"issue_comment action={action} not gated"}

    issue = payload.get("issue") or {}
    if not issue.get("pull_request"):
        return {"status": "no_op", "reason": "issue_comment on non-PR"}

    comment = payload.get("comment") or {}
    body = comment.get("body") or ""
    if not _RECHECK_PAT.search(body):
        return {"status": "no_op", "reason": "no /grug recheck trigger"}

    repo = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    sender = payload.get("sender") or {}

    installation_id = installation.get("id")
    owner = (repo.get("owner") or {}).get("login") or repo.get("full_name", "").split("/")[0]
    repo_name = repo.get("name")
    pr_number = issue.get("number")
    sender_login = sender.get("login", "")
    pr_author_login = (issue.get("user") or {}).get("login", "")

    if not all([installation_id, owner, repo_name, pr_number]):
        log.warning(
            "issue_comment_payload_incomplete",
            extra={
                "installation_id": installation_id, "owner": owner,
                "repo_name": repo_name, "pr_number": pr_number,
            },
        )
        return {"status": "skip", "reason": "incomplete_payload"}

    if not is_install_allowlisted(int(installation_id)):
        return {"status": "no_op", "reason": "installer not allowlisted"}

    repo_id = repo.get("id")
    if repo_id is not None and not is_persona_enabled(
        int(installation_id), int(repo_id), "tpm",
    ):
        return {"status": "no_op", "reason": "tpm disabled for this repo"}

    # Author OR write+ collaborator only — public-listed App means random
    # commenters can't spam re-evaluations. Lazy imports keep cold-start
    # cheap when only PR events fire.
    from github_app_auth import with_install_token_retry  # type: ignore
    from personas.tpm.persona import evaluate_pull_request  # type: ignore
    import httpx  # type: ignore

    if sender_login != pr_author_login:
        def _check_perm(token: str) -> str:
            r = httpx.get(
                f"https://api.github.com/repos/{owner}/{repo_name}"
                f"/collaborators/{sender_login}/permission",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10,
            )
            r.raise_for_status()
            return (r.json() or {}).get("permission", "")

        try:
            perm = with_install_token_retry(int(installation_id), _check_perm)
        except httpx.HTTPStatusError as e:
            log.warning(
                "recheck_perm_lookup_failed",
                extra={"sender": sender_login, "status": e.response.status_code},
            )
            return {"status": "skip", "reason": "perm_lookup_failed"}

        if perm not in _AUTHORIZED_ROLES:
            log.info(
                "recheck_unauthorized",
                extra={
                    "sender": sender_login, "perm": perm, "owner": owner,
                    "repo": repo_name, "pr_number": pr_number,
                },
            )
            return {"status": "no_op", "reason": "sender lacks write perm"}

    # Re-fetch PR to get current head_sha + body (issue payload only has
    # the issue mirror, not the PR head). One API call per recheck.
    def _fetch_pr(token: str) -> dict[str, Any]:
        r = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo_name}/pulls/{pr_number}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    try:
        pr = with_install_token_retry(int(installation_id), _fetch_pr)
    except httpx.HTTPStatusError as e:
        log.warning(
            "recheck_pr_fetch_failed",
            extra={"pr_number": pr_number, "status": e.response.status_code},
        )
        return {"status": "skip", "reason": "pr_fetch_failed"}

    head_sha = ((pr.get("head") or {}).get("sha")) or ""
    pr_body = pr.get("body") or ""
    if not head_sha:
        return {"status": "skip", "reason": "pr_has_no_head_sha"}

    result = evaluate_pull_request(
        installation_id=int(installation_id),
        owner=owner,
        repo=repo_name,
        head_sha=head_sha,
        pr_body=pr_body,
        pr_number=int(pr_number),
    )
    log.info(
        "recheck_dispatched",
        extra={
            "owner": owner, "repo": repo_name, "pr_number": pr_number,
            "sender": sender_login, "result": "pass" if result.passed else "fail",
        },
    )
    return {
        "status": "dispatched",
        "persona": "tpm",
        "trigger": "recheck",
        "result": "pass" if result.passed else "fail",
    }
