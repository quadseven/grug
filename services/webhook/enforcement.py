# MIRRORED — sibling at services/api/enforcement.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Enforcement lifecycle — create/delete Grug-managed rulesets.

Called from dispatcher.py (on installation) and installations.py (on
persona toggle). Functions take install_token directly — callers wrap
with with_install_token_retry.
"""

from __future__ import annotations

import logging
from typing import Any

from github_rulesets_client import (
    EnforcementState,
    GRUG_RULESET_PREFIX,
    create_ruleset,
    delete_ruleset,
    detect_enforcement,
    list_rulesets,
)

log = logging.getLogger("grug.enforcement")

GRUG_TPM_RULESET_NAME = "Grug — TPM Enforcement"
GRUG_DOR_CHECK_NAME = "Grug — Definition of Ready"


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
    state = detect_enforcement(
        install_token, owner, repo, default_branch, GRUG_DOR_CHECK_NAME,
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

    if ruleset_id is None:
        rulesets = list_rulesets(install_token, owner, repo)
        for rs in rulesets:
            if rs.get("name", "").startswith(GRUG_RULESET_PREFIX):
                ruleset_id = rs["id"]
                break

    if ruleset_id is None:
        log.info(
            "enforcement_nothing_to_remove",
            extra={"owner": owner, "repo": repo,
                   "install_id": install_id, "repo_id": repo_id},
        )
        return

    delete_ruleset(install_token, owner, repo, ruleset_id)
    set_enforcement_id(install_id, repo_id, None)

    log.info(
        "enforcement_deleted",
        extra={
            "owner": owner, "repo": repo,
            "install_id": install_id, "repo_id": repo_id,
            "ruleset_id": ruleset_id,
        },
    )
