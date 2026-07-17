"""Enforcement lifecycle — create/delete Grug-managed rulesets.

Called from dispatcher.py (on installation) and installations.py (on
persona toggle). Functions take install_token directly — callers wrap
with with_install_token_retry.
"""

from __future__ import annotations

import logging

from github_rulesets_client import (
    EnforcementState,
    create_ruleset,
    delete_ruleset,
    detect_enforcement,
    list_rulesets,
)

from personas.tribe import (
    CHECK_CHIEF,
    LEGACY_RULESET_CHIEF,
    RULESET_CHIEF,
    is_enforcement_ruleset_name,
)

log = logging.getLogger("grug.enforcement")

# Canonical tribe names (personas.tribe). Legacy aliases re-exported so
# existing imports keep working during the cutover.
GRUG_TPM_RULESET_NAME = RULESET_CHIEF
GRUG_DOR_CHECK_NAME = CHECK_CHIEF
# Back-compat spellings for scripts / tests that imported the old literals.
LEGACY_TPM_RULESET_NAME = LEGACY_RULESET_CHIEF


def ensure_enforcement(
    install_token: str,
    owner: str,
    repo: str,
    default_branch: str,
    install_id: int,
    repo_id: int,
) -> EnforcementState:
    """Create a Grug-managed ruleset if no enforcement exists. Idempotent.

    Returns the resulting enforcement state after the operation.
    """
    from adapters.install_store import get_enforcement_id  # type: ignore
    state = detect_enforcement(
        install_token, owner, repo, default_branch, GRUG_DOR_CHECK_NAME,
        stored_ruleset_id=get_enforcement_id(install_id, repo_id),
    )
    if state != "none":
        log.info(
            "enforcement_already_present",
            extra={
                "owner": owner, "repo": repo,
                "install_id": install_id, "repo_id": repo_id,
                "state": state,
            },
        )
        from observability import emit_enforcement_metric  # type: ignore
        emit_enforcement_metric(f"{owner}/{repo}", state)
        return state

    result = create_ruleset(
        install_token, owner, repo,
        GRUG_TPM_RULESET_NAME, [GRUG_DOR_CHECK_NAME],
    )
    ruleset_id = result["id"]

    from adapters.install_store import set_enforcement_id  # type: ignore
    set_enforcement_id(install_id, repo_id, ruleset_id)

    log.info(
        "enforcement_created",
        extra={
            "owner": owner, "repo": repo,
            "install_id": install_id, "repo_id": repo_id,
            "ruleset_id": ruleset_id,
        },
    )
    from observability import emit_enforcement_metric  # type: ignore
    emit_enforcement_metric(f"{owner}/{repo}", "grug_managed")
    return "grug_managed"


def heal_enforcement(
    install_token: str,
    owner: str,
    repo: str,
    default_branch: str,
    install_id: int,
    repo_id: int,
    *,
    old_ruleset_id: int,
) -> EnforcementState:
    """Re-create a Grug-managed ruleset after external deletion.

    Clears the stale enforcement_ruleset_id first, then delegates to
    ensure_enforcement for idempotent re-creation.
    """
    from adapters.install_store import set_enforcement_id  # type: ignore
    set_enforcement_id(install_id, repo_id, None)

    new_state = ensure_enforcement(
        install_token, owner, repo, default_branch, install_id, repo_id,
    )

    if new_state == "grug_managed":
        from adapters.install_store import get_enforcement_id  # type: ignore
        new_ruleset_id = get_enforcement_id(install_id, repo_id)
        log.info(
            "enforcement_healed",
            extra={
                "owner": owner, "repo": repo,
                "install_id": install_id, "repo_id": repo_id,
                "old_ruleset_id": old_ruleset_id,
                "new_ruleset_id": new_ruleset_id,
            },
        )

    return new_state


def remove_enforcement(
    install_token: str,
    owner: str,
    repo: str,
    install_id: int,
    repo_id: int,
) -> None:
    """Delete the Grug-managed ruleset if one exists.

    Reads the stored ruleset_id from DDB first. If not stored, falls
    back to listing rulesets and finding by name prefix.
    """
    from adapters.install_store import get_enforcement_id, set_enforcement_id  # type: ignore

    ruleset_id = get_enforcement_id(install_id, repo_id)

    # Collect every ruleset to delete. With a stored ID we trust it; without,
    # we fall back to matching the EXACT names grug's own Chief enforcement
    # ruleset has used (canonical + legacies) - NOT a broad "Grug - " prefix,
    # which would also delete an unrelated user ruleset that merely shares the
    # prefix. Delete ALL exact matches, not just the first: during the
    # nomenclature cutover a canonical and a legacy enforcement ruleset can
    # coexist, and deleting only one would leave the other active + orphaned.
    to_delete: list = []
    if ruleset_id is not None:
        to_delete = [ruleset_id]
    else:
        rulesets = list_rulesets(install_token, owner, repo)
        to_delete = [
            rs["id"]
            for rs in rulesets
            if is_enforcement_ruleset_name(rs.get("name", ""))
        ]

    if not to_delete:
        log.info(
            "enforcement_nothing_to_remove",
            extra={"owner": owner, "repo": repo,
                   "install_id": install_id, "repo_id": repo_id},
        )
        return

    for rid in to_delete:
        delete_ruleset(install_token, owner, repo, rid)
    set_enforcement_id(install_id, repo_id, None)

    log.info(
        "enforcement_deleted",
        extra={
            "owner": owner, "repo": repo,
            "install_id": install_id, "repo_id": repo_id,
            "ruleset_ids": to_delete,
        },
    )
    from observability import emit_enforcement_metric  # type: ignore
    emit_enforcement_metric(f"{owner}/{repo}", "none")
