"""Webhook → persona dispatch.

Routes GitHub events to active personas. Handles:
  - installation / installation_repositories: record + delete INST# rows
  - pull_request: iterates the persona registry (ADR-0010) — every
    registered persona whose `events` include pull_request dispatches
    through the uniform `dispatch_pull_request(ctx)` seam
  - issue_comment: the `/grug recheck` slash command
  - repository_ruleset: enforcement self-healing

Allowlist gate (Slice 5 #26): non-allowlisted installs no_op silently
BEFORE persona work runs. Defense-in-depth — App registration is
public but webhook never acts on PRs from non-allowlisted users.
"""

from __future__ import annotations

import copy
import importlib
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
from personas import registry as persona_registry

log = logging.getLogger("grug.webhook.dispatcher")


def dispatch(
    event_name: str, payload: dict[str, Any], *, delivery_id: str = "",
) -> dict[str, str]:
    """Route a webhook event to its persona handlers. Returns audit dict.

    `delivery_id` is the GitHub `X-GitHub-Delivery` UUID; it is threaded
    to the pull_request handler so the async Elder offload (#272) can key
    its idempotency claim on it. Defaults to "" for non-PR events (which
    don't enqueue async work) and for older call sites/tests.
    """
    if event_name == "installation":
        return _handle_installation(payload)
    if event_name == "installation_repositories":
        # Repo-list change on an existing install — install row already
        # exists; per-repo config lives in services/api/installations.py.
        return {"status": "no_op", "reason": "installation_repositories acknowledged"}
    if event_name == "pull_request":
        return _handle_pull_request(payload, delivery_id=delivery_id)
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
    payload: dict[str, Any], *, delivery_id: str = "",
) -> dict[str, Any]:
    """Returns either a short {status, reason} dict (allowlist or
    payload skip) OR an aggregated {status, personas: [...]} dict
    when at least one persona dispatched. The return-type union is
    intentional — dispatcher consumers only read `.get("status")` and
    optional list-shaped `.get("personas")`. Honest `dict[str, Any]`
    over the prior `dict[str, str]` + `# type: ignore`."""
    action = payload.get("action", "")
    # The union of every registered persona's actions ("closed" joined via
    # Warder, #471). Per-persona filtering happens in the loop; anything
    # outside the union no_ops early exactly as before.
    if not any(action in spec.actions for spec in persona_registry.REGISTRY):
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

    # Allowlist gate — defense-in-depth — non-allowlisted
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

    # `get_repo_config` is already imported at module scope (top of file);
    # a function-local re-import would shadow it and silently defeat
    # `patch("dispatcher.get_repo_config")` in tests (#272).
    repo_id = repo.get("id")

    # Registry dispatch loop (ADR-0010): every registered persona whose
    # events include pull_request dispatches from this handler on the
    # same event, producing INDEPENDENT verdicts. Registry order is
    # dispatch order (TPM first, Elder second). Each persona call is
    # isolated in its own try/except so a bug or import failure in one
    # doesn't 500 the webhook or starve the others (#185).
    results: list[dict[str, str]] = []
    # An UNEXPECTED exception escaping an ASYNC persona's dispatch is a
    # HANDOFF failure (the review was never durably enqueued) - unlike an
    # inline persona's publish, swallowing it into a 200 drops the review
    # with no GitHub redelivery (codex peer-review HIGH, PR #477). We run
    # every persona for isolation, then re-raise the first async-handoff
    # error AFTER the loop so the delivery is non-2xx and GitHub retries.
    async_handoff_error: Exception | None = None
    for spec in persona_registry.REGISTRY:
        if "pull_request" not in spec.events:
            continue
        if action not in spec.actions:
            # e.g. Warder only wakes on "closed"; the update personas
            # don't. Silent skip - costs no store read, not a disable.
            continue

        if repo_id is None:
            # Rare payload-shape glitch: no repository.id. What that
            # means is the persona's own declared policy (Chief: run
            # anyway — a missing id must not skip DoR; Elder: skip —
            # never run an LLM review blind).
            enabled = spec.missing_repo_policy == "enabled"
        else:
            enabled = is_persona_enabled(
                int(installation_id), int(repo_id), spec.key,
            )
        if not enabled:
            # Distinguishable from "persona ran and found nothing."
            log.info(
                "persona_disabled_skip",
                extra={
                    "installation_id": installation_id,
                    "owner": owner, "repo": repo_name, "pr_number": pr_number,
                    "persona": spec.key,
                    "reason": "no_repo_id" if repo_id is None else "toggle_off",
                },
            )
            continue

        # Future-persona edge (unreachable with today's registry): a
        # persona with missing_repo_policy="enabled" AND a blocking_flag
        # dispatches with blocking_default on a missing repo_id - the
        # cfg read below needs the id. Deliberate, not an oversight.
        blocking = spec.blocking_default
        if spec.blocking_flag is not None and repo_id is not None:
            # One config read, only for personas with a blocking mode —
            # same store call pattern as the hand-wired era (zero reads
            # for TPM, one for an enabled Elder).
            cfg = get_repo_config(int(installation_id), int(repo_id))
            blocking = bool(cfg.get(spec.blocking_flag, spec.blocking_default))

        # Each persona gets its OWN deep copy of the payload (audit #477
        # H2 / codex peer-review): the isolation guarantee is structural,
        # not by-convention, so a future persona that mutates ctx.payload
        # (normalize/redact/pop) cannot corrupt what later personas - or
        # Elder's async enqueue - receive. N deep copies of one webhook
        # payload on the ACK path is negligible vs the LLM enqueue it
        # already does; correctness over the micro-cost.
        ctx = persona_registry.PullRequestContext(
            installation_id=int(installation_id),
            owner=owner,
            repo_name=repo_name,
            head_sha=head_sha,
            pr_number=int(pr_number),
            pr_body=pr_body,
            payload=copy.deepcopy(payload),
            delivery_id=delivery_id,
            blocking=blocking,
        )
        try:
            module = importlib.import_module(spec.dispatch_module)
            results.append(module.dispatch_pull_request(ctx))
        except Exception as e:  # noqa: BLE001 - per-persona isolation guard
            # Isolate so one persona's failure never starves the others
            # (#185). delivery_id + kind keep the log correlatable to the
            # GitHub delivery GUID. For an INLINE persona this 200s (a
            # retry would duplicate its already-done publish); for an
            # ASYNC persona it is a dropped handoff and we re-raise below.
            log.error(
                "persona_dispatch_unhandled",
                extra={
                    "installation_id": installation_id,
                    "owner": owner, "repo": repo_name, "pr_number": pr_number,
                    "persona": spec.key,
                    "delivery_id": delivery_id,
                    "head_sha": head_sha[:8],
                    "kind": type(e).__name__,
                    "dispatch_style": spec.dispatch_style,
                },
                exc_info=True,
            )
            results.append({"persona": spec.key, "result": "unhandled_error"})
            if spec.dispatch_style == "async" and async_handoff_error is None:
                async_handoff_error = e

    if async_handoff_error is not None:
        # Re-raise so the webhook returns non-2xx and GitHub redelivers
        # the event - the durable retry the async handoff needs. Inline
        # personas already ran; their publishes are idempotent per
        # head_sha on redelivery (parity with the pre-registry behavior,
        # where an Elder enqueue exception 500ed after TPM published).
        # NOTE: the EXPECTED enqueue-returns-False path does NOT raise -
        # it returns `enqueue_failed` and 200s, preserving the deliberate
        # #272 "drop + re-trigger on next push" contract.
        raise async_handoff_error

    if not results:
        return {"status": "no_op", "reason": "all personas disabled"}
    return {"status": "dispatched", "personas": results}


# NOTE (#465, ADR-0010): the former `_dispatch_tpm` body lives in
# `personas/tpm/webhook_dispatch.py`, and the former Elder enqueue block
# in `personas/code_reviewer/webhook_dispatch.py` — the registry loop
# above resolves each persona's module from its PersonaSpec. The Elder
# review itself still executes off the ACK path via
# `async_dispatch.run_elder_job` (#272), which calls
# `dispatch_code_review` directly with its own broad final-guard.


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
    comment_id = comment.get("id")
    from grug_commands import parse_command  # type: ignore
    cmd = parse_command(body)
    if cmd is None:
        return {"status": "no_op", "reason": "no /grug command trigger"}

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

    # Interactive commands (#528) reuse the SAME gating above (allowlist +
    # tpm + author-or-write-collaborator), then branch to their own actions.
    # recheck keeps the original DoR-re-evaluate path below.
    if cmd.verb != "recheck":
        from interactive import run_command  # type: ignore
        return run_command(
            cmd.verb, cmd.arg, install_id=int(installation_id), owner=owner,
            repo=repo_name, pr_number=int(pr_number),
            comment_id=int(comment_id) if comment_id is not None else 0,
            token_fn=lambda fn: with_install_token_retry(int(installation_id), fn),
        )

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
