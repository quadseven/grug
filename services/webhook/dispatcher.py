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
    get_repo_config,
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
        # exists; per-repo config lives in services/api/installations.py.
        return {"status": "no_op", "reason": "installation_repositories acknowledged"}
    if event_name == "pull_request":
        return _handle_pull_request(payload)
    if event_name == "pull_request_review":
        return {"status": "no_op", "reason": "no persona for pull_request_review yet"}
    if event_name == "issue_comment":
        return _handle_issue_comment(payload)
    if event_name == "repository_ruleset":
        return _handle_repository_ruleset(payload)
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

        if is_install_allowlisted(int(install_id)):
            _enforce_on_repos(int(install_id), payload.get("repositories") or [])

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


def _handle_repository_ruleset(payload: dict[str, Any]) -> dict[str, str]:
    action = payload.get("action", "")
    if action != "deleted":
        return {"status": "no_op", "reason": f"repository_ruleset action={action} not gated"}

    ruleset = payload.get("repository_ruleset") or {}
    ruleset_name = ruleset.get("name", "")
    ruleset_id = ruleset.get("id")

    from github_rulesets_client import GRUG_RULESET_PREFIX  # type: ignore
    if not ruleset_name.startswith(GRUG_RULESET_PREFIX):
        return {"status": "no_op", "reason": "not grug-managed ruleset"}

    repo = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    install_id = installation.get("id")
    repo_id = repo.get("id")
    owner = (repo.get("owner") or {}).get("login", "")
    repo_name = repo.get("name", "")
    default_branch = repo.get("default_branch", "main")

    if not all([install_id, repo_id, owner, repo_name]):
        return {"status": "skip", "reason": "incomplete_payload"}

    if not is_install_allowlisted(int(install_id)):
        return {"status": "no_op", "reason": "installer not allowlisted"}

    if not is_persona_enabled(int(install_id), int(repo_id), "tpm"):
        log.info(
            "self_heal_skip_tpm_disabled",
            extra={"install_id": install_id, "repo": f"{owner}/{repo_name}"},
        )
        return {"status": "no_op", "reason": "tpm disabled for this repo"}

    cfg = get_repo_config(int(install_id), int(repo_id))
    if cfg.get("force_disable_enforcement", False):
        log.info(
            "self_heal_skip_force_disable",
            extra={"install_id": install_id, "repo": f"{owner}/{repo_name}"},
        )
        return {"status": "no_op", "reason": "force_disable_enforcement is set"}

    _heal_enforcement_on_repo(
        int(install_id), int(repo_id), owner, repo_name,
        default_branch, int(ruleset_id),
    )
    return {"status": "healed", "old_ruleset_id": str(ruleset_id)}


def _heal_enforcement_on_repo(
    install_id: int, repo_id: int, owner: str, repo_name: str,
    default_branch: str, old_ruleset_id: int,
) -> None:
    from github_app_auth import with_install_token_retry  # type: ignore
    from enforcement import heal_enforcement  # type: ignore

    try:
        with_install_token_retry(
            install_id,
            lambda token, o=owner, r=repo_name, db=default_branch, iid=install_id, rid=repo_id, old=old_ruleset_id: (
                heal_enforcement(token, o, r, db, iid, rid, old_ruleset_id=old)
            ),
        )
    except Exception:
        log.warning(
            "self_heal_failed",
            extra={"install_id": install_id, "repo": f"{owner}/{repo_name}",
                   "old_ruleset_id": old_ruleset_id},
            exc_info=True,
        )


def _enforce_on_repos(install_id: int, repositories: list[dict]) -> None:
    """Best-effort enforcement creation for repos in an install payload."""
    from github_app_auth import with_install_token_retry  # type: ignore
    from enforcement import ensure_enforcement  # type: ignore

    for repo in repositories:
        repo_id = repo.get("id")
        full_name = repo.get("full_name", "")
        default_branch = repo.get("default_branch", "main")
        parts = full_name.split("/", 1)
        if len(parts) != 2 or not repo_id:
            continue
        owner, repo_name = parts
        try:
            with_install_token_retry(
                install_id,
                lambda token, o=owner, r=repo_name, db=default_branch, iid=install_id, rid=int(repo_id): (
                    ensure_enforcement(token, o, r, db, iid, rid)
                ),
            )
        except Exception:
            log.warning(
                "enforcement_create_failed",
                extra={"install_id": install_id, "repo": full_name},
                exc_info=True,
            )


def _handle_pull_request(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Returns either a short {status, reason} dict (allowlist or
    payload skip) OR an aggregated {status, personas: [...]} dict
    when at least one persona dispatched. The return-type union is
    intentional — dispatcher consumers only read `.get("status")` and
    optional list-shaped `.get("personas")`. Honest `dict[str, Any]`
    over the prior `dict[str, str]` + `# type: ignore`."""
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

    # Lazy import — keeps cold-start cheap when only non-PR events fire
    import httpx  # type: ignore
    from adapters.install_store import get_repo_config  # type: ignore

    repo_id = repo.get("id")

    # Both TPM and Elder dispatch from this handler on the same event,
    # producing INDEPENDENT verdicts. One failing must not skip the
    # other — that's the load-bearing property the test suite asserts.
    # Each persona's exceptions are caught locally so a transient GH
    # 5xx on one doesn't 500 the webhook or starve the other.
    results: list[dict[str, str]] = []

    tpm_enabled = (
        repo_id is None
        or is_persona_enabled(int(installation_id), int(repo_id), "tpm")
    )
    if tpm_enabled:
        results.append(_dispatch_tpm(
            installation_id=int(installation_id),
            owner=owner, repo_name=repo_name,
            head_sha=head_sha, pr_number=int(pr_number),
            pr_body=pr_body,
        ))
    else:
        log.info(
            "persona_disabled_skip",
            extra={
                "installation_id": installation_id,
                "owner": owner, "repo": repo_name, "pr_number": pr_number,
                "persona": "tpm",
            },
        )

    code_reviewer_enabled = (
        repo_id is not None
        and is_persona_enabled(
            int(installation_id), int(repo_id), "code_reviewer",
        )
    )
    if code_reviewer_enabled:
        # `code_reviewer_blocking` controls advisory-vs-blocking mode.
        # Defaults False; operator flips via dashboard once trust is
        # established. Fetched alongside the toggle since both come
        # from the same RepoConfig row.
        cfg = get_repo_config(int(installation_id), int(repo_id))
        results.append(_dispatch_code_reviewer(
            payload=payload,
            installation_id=int(installation_id),
            owner=owner, repo_name=repo_name, pr_number=int(pr_number),
            blocking=bool(cfg.get("code_reviewer_blocking", False)),
        ))
    else:
        # Either explicitly disabled per-repo OR `repo_id is None`
        # (rare payload-shape glitch). Both produce a `disabled_skip`
        # log so the operator can tell the difference from "Elder ran
        # and found nothing." TPM treats missing repo_id as enabled by
        # legacy — the asymmetry is deliberate: Elder requires repo_id
        # to call is_persona_enabled, so a missing one is treated as
        # opt-out rather than blanket-enabled.
        log.info(
            "persona_disabled_skip",
            extra={
                "installation_id": installation_id,
                "owner": owner, "repo": repo_name, "pr_number": pr_number,
                "persona": "code_reviewer",
                "reason": "no_repo_id" if repo_id is None else "toggle_off",
            },
        )

    if not results:
        return {"status": "no_op", "reason": "all personas disabled"}
    return {"status": "dispatched", "personas": results}


def _dispatch_tpm(
    *, installation_id: int, owner: str, repo_name: str, head_sha: str,
    pr_number: int, pr_body: str,
) -> dict[str, str]:
    """TPM persona dispatch — pure evaluate + publish. Catches publish
    errors locally so the Elder dispatch can still run."""
    import httpx  # type: ignore
    from personas.tpm.persona import (  # type: ignore
        evaluate_pull_request, publish_tpm_evaluation,
    )

    evaluation = evaluate_pull_request(pr_body)
    try:
        publish_tpm_evaluation(
            evaluation,
            installation_id=installation_id,
            owner=owner,
            repo=repo_name,
            head_sha=head_sha,
            pr_number=pr_number,
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            "tpm_publish_failed",
            extra={
                "installation_id": installation_id,
                "owner": owner,
                "repo": repo_name,
                "pr_number": pr_number,
                "head_sha": head_sha[:8],
                "kind": type(e).__name__,
                "status": getattr(getattr(e, "response", None), "status_code", None),
            },
        )
        return {"persona": "tpm", "result": "publish_failed"}
    return {
        "persona": "tpm",
        "result": "pass" if evaluation.passed else "fail",
    }


def _dispatch_code_reviewer(
    *, payload: dict[str, Any], installation_id: int, owner: str,
    repo_name: str, pr_number: int, blocking: bool,
) -> dict[str, str]:
    """Elder persona dispatch — fetch+parse+LLM+evaluate+publish.

    Catches every exception locally and degrades to a "skipped" result
    rather than propagating. Advisory-first contract is enforced
    inside `dispatch_code_review`; this wrapper exists to keep TPM and
    Elder dispatches symmetric in `_handle_pull_request`.
    """
    from personas.code_reviewer.dispatch import (  # type: ignore
        dispatch_code_review,
    )
    # Final guard MUST be broad — TPM has already dispatched by the
    # time this runs, and propagating an Elder exception would 500
    # the webhook with no way to surface TPM's result. The
    # `exc_info=True` log carries the full traceback to DD/Sentry, so
    # unknown exception types are not buried, just not propagated.
    try:
        return dispatch_code_review(payload, blocking=blocking)
    except Exception as e:  # noqa: BLE001 — explicit final guard
        log.error(
            "code_review_dispatch_unhandled",
            extra={
                "installation_id": installation_id,
                "owner": owner, "repo": repo_name, "pr_number": pr_number,
                "kind": type(e).__name__,
            },
            exc_info=True,
        )
        return {"persona": "code_reviewer", "result": "unhandled_error"}


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
    from personas.tpm.persona import evaluate_pull_request, publish_tpm_evaluation  # type: ignore
    import httpx  # type: ignore

    # URL-encode user-controlled path components. GitHub repo + login
    # rules forbid most URL-special chars but `+`, `.`, etc. round-trip
    # safely; defensive `quote(safe="")` guards against future weirdness
    # + matches the async-blocker-hunter agent's "URL-encoding gaps in
    # interpolated query strings" pattern.
    from urllib.parse import quote as _q

    if sender_login != pr_author_login:
        def _check_perm(token: str) -> str:
            r = httpx.get(
                f"https://api.github.com/repos/{_q(owner, safe='')}/"
                f"{_q(repo_name, safe='')}/collaborators/"
                f"{_q(sender_login, safe='')}/permission",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10,
            )
            r.raise_for_status()
            return (r.json() or {}).get("permission", "")

        # Catch BOTH HTTPStatusError (4xx/5xx from GH) AND RequestError
        # (transport: timeout, DNS, connection-reset). Earlier code only
        # caught HTTPStatusError; transport blips surfaced as webhook
        # 500 → GitHub retries the delivery → duplicate work.
        # async-blocker-hunter F-01.
        try:
            perm = with_install_token_retry(int(installation_id), _check_perm)
        except httpx.HTTPStatusError as e:
            log.warning(
                "recheck_perm_lookup_failed",
                extra={"sender": sender_login, "status": e.response.status_code},
            )
            return {"status": "skip", "reason": "perm_lookup_failed"}
        except httpx.RequestError as e:
            log.warning(
                "recheck_perm_lookup_transport_failed",
                extra={"sender": sender_login, "kind": type(e).__name__},
            )
            return {"status": "skip", "reason": "perm_lookup_transport_failed"}

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
            f"https://api.github.com/repos/{_q(owner, safe='')}/"
            f"{_q(repo_name, safe='')}/pulls/{int(pr_number)}",
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
    except httpx.RequestError as e:
        # Transport-level failure (timeout, DNS, connection-reset).
        # Same rationale as the perm-lookup catch above. F-01.
        log.warning(
            "recheck_pr_fetch_transport_failed",
            extra={"pr_number": pr_number, "kind": type(e).__name__},
        )
        return {"status": "skip", "reason": "pr_fetch_transport_failed"}

    head_sha = ((pr.get("head") or {}).get("sha")) or ""
    pr_body = pr.get("body") or ""
    if not head_sha:
        return {"status": "skip", "reason": "pr_has_no_head_sha"}

    evaluation = evaluate_pull_request(pr_body)
    # Same wrap pattern as the pull_request handler — peer-review CRITICAL.
    try:
        publish_tpm_evaluation(
            evaluation,
            installation_id=int(installation_id),
            owner=owner,
            repo=repo_name,
            head_sha=head_sha,
            pr_number=int(pr_number),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            "recheck_publish_failed",
            extra={
                "installation_id": int(installation_id),
                "owner": owner,
                "repo": repo_name,
                "pr_number": int(pr_number),
                "head_sha": head_sha[:8],
                "kind": type(e).__name__,
                "status": getattr(getattr(e, "response", None), "status_code", None),
            },
        )
        return {"status": "skip", "trigger": "recheck", "reason": "publish_failed"}
    log.info(
        "recheck_dispatched",
        extra={
            "owner": owner, "repo": repo_name, "pr_number": pr_number,
            "sender": sender_login, "result": "pass" if evaluation.passed else "fail",
        },
    )
    return {
        "status": "dispatched",
        "persona": "tpm",
        "trigger": "recheck",
        "result": "pass" if evaluation.passed else "fail",
    }
