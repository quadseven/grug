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
    get_ruleset,
    list_rulesets,
    update_ruleset,
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


def migrate_check_context(
    install_token: str,
    owner: str,
    repo: str,
    ruleset_id: int,
) -> bool:
    """Heal a Grug-managed ruleset whose required_status_checks context is
    a stale legacy alias (e.g. a pre-rename em-dash title) instead of the
    current canonical check name.

    Every check-name rename (Chief/Hunt Plan cutover, and any future one)
    ships a dual-post insurance mirror in github_checks_client so old
    rulesets keep passing - but nothing previously updated the ruleset's
    OWN required-check context to the new canonical name, so an
    already-enrolled repo stayed pinned to the old title forever (and,
    for the earliest em-dash titles, to a non-ASCII check name visible in
    the GitHub UI). Returns True if the ruleset was updated, False if it
    already names the canonical check.

    Rewrites ONLY known legacy aliases of a Grug persona check (Chief,
    Elder, Guard, ...) to that check's canonical name; every other
    required context is preserved unchanged (Qodo on #685: an earlier
    version replaced the whole checks list with a Chief-only singleton,
    which would have silently dropped any other required check a ruleset
    carries). Also dedupes so a ruleset that somehow ended up with both
    the canonical name and a stale alias collapses to one entry. Inspects
    every required_status_checks rule on the ruleset, not just the first
    (Qodo #685: GitHub does not document a one-rule-per-type limit).

    Sends update_ruleset the ruleset's FULL `rules` array with only the
    matching required_status_checks rule(s)' contexts changed - every
    other rule (and that rule's other parameters, e.g.
    strict_required_status_checks_policy) passes through byte-for-byte
    (CodeRabbit #685: PUT /rulesets/{id} is not a documented partial-update
    endpoint, so a body built from a synthesized single rule risks
    silently dropping any OTHER rule type an admin added to the same
    ruleset).

    Within required_status_checks itself, an entry whose context is
    already canonical (or isn't a known legacy alias at all) is kept
    byte-for-byte, EXCEPT a null `integration_id` is always dropped
    (Qodo #685: GitHub 422s the whole PUT on integration_id: null,
    including on an untouched entry re-sent verbatim - null and absent
    mean the same thing to GitHub's model, so this never changes what the
    entry actually requires). A rewritten entry keeps a real
    (non-null) `integration_id` unchanged (CodeRabbit #685: an earlier
    version rebuilt every entry as bare {"context": ...}, silently
    dropping that scoping and collapsing same-named checks from different
    integrations). A non-string context is left alone entirely, not
    passed to primary_check_name (Qodo #685: a malformed entry must not
    make the healed PUT itself malformed).
    """
    from personas.tribe import primary_check_name

    ruleset = get_ruleset(install_token, owner, repo, ruleset_id)
    rules = ruleset.get("rules", [])
    new_rules: list[dict] = []
    changed = False
    for rule in rules:
        if rule.get("type") != "required_status_checks":
            new_rules.append(rule)
            continue
        old_checks = rule.get("parameters", {}).get("required_status_checks", [])
        if not old_checks:
            new_rules.append(rule)
            continue
        new_checks: list[dict] = []
        seen: set[tuple] = set()
        rule_changed = False
        for check in old_checks:
            ctx = check.get("context")
            canonical = primary_check_name(ctx) if isinstance(ctx, str) else ctx
            new_check = check if canonical == ctx else {**check, "context": canonical}
            if new_check.get("integration_id") is None and "integration_id" in new_check:
                new_check = {k: v for k, v in new_check.items() if k != "integration_id"}
                rule_changed = True
            if canonical != ctx:
                rule_changed = True
            try:
                key = (canonical, new_check.get("integration_id"))
                duplicate = key in seen
            except TypeError:
                # Unhashable context (a malformed entry - e.g. dict/list,
                # not the str/None this ruleset shape is meant to carry):
                # can't meaningfully dedupe it, so never treat it as one.
                key = None
                duplicate = False
            if duplicate:
                rule_changed = True  # dedup drop
                continue
            if key is not None:
                seen.add(key)
            new_checks.append(new_check)
        if not rule_changed:
            new_rules.append(rule)
            continue
        changed = True
        new_rules.append({
            **rule,
            "parameters": {
                **rule.get("parameters", {}),
                "required_status_checks": new_checks,
            },
        })
    if not changed:
        return False
    update_ruleset(install_token, owner, repo, ruleset_id, new_rules)
    return True


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
    stored_ruleset_id = get_enforcement_id(install_id, repo_id)
    state = detect_enforcement(
        install_token, owner, repo, default_branch, GRUG_DOR_CHECK_NAME,
        stored_ruleset_id=stored_ruleset_id,
    )
    if state != "none":
        if state == "grug_managed":
            ruleset_id = stored_ruleset_id
            if ruleset_id is not None:
                try:
                    if migrate_check_context(install_token, owner, repo, ruleset_id):
                        log.info(
                            "enforcement_check_context_healed",
                            extra={
                                "owner": owner, "repo": repo,
                                "install_id": install_id, "repo_id": repo_id,
                                "ruleset_id": ruleset_id,
                            },
                        )
                except Exception as e:  # noqa: BLE001 - self-heal never blocks the existing-state return
                    log.warning(
                        "enforcement_check_context_heal_failed",
                        extra={
                            "owner": owner, "repo": repo,
                            "install_id": install_id, "repo_id": repo_id,
                            "ruleset_id": ruleset_id,
                            "kind": type(e).__name__,
                            "detail": str(e)[:200],
                        },
                    )
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

    # Collect every ruleset to delete. ALWAYS scan by exact name in addition
    # to the stored ID and take the UNION: during the nomenclature cutover a
    # canonical (stored-ID) enforcement ruleset and a legacy em-dash one can
    # coexist, so deleting only the stored ID would leave the legacy ruleset
    # active + orphaned - still gating merges while the store/UI report
    # enforcement removed. Match the EXACT names grug's own Chief enforcement
    # has used (canonical + legacies), NOT a broad "Grug - " prefix, so an
    # unrelated user ruleset that merely shares the prefix is never touched.
    to_delete_ids: set = set()
    if ruleset_id is not None:
        to_delete_ids.add(ruleset_id)
    # The name scan is a best-effort SUPPLEMENT to catch a coexisting legacy
    # ruleset; it must never block deleting a known stored ID. If listing
    # fails (GitHub 5xx / rate limit) we still delete the stored ID and pick
    # up any legacy orphan on the next disable/reconcile. Only when there is
    # NO stored ID does a listing failure leave us nothing safe to do - then
    # re-raise so the caller retries rather than silently reporting removed.
    try:
        rulesets = list_rulesets(install_token, owner, repo)
        for rs in rulesets:
            if is_enforcement_ruleset_name(rs.get("name", "")):
                to_delete_ids.add(rs["id"])
    except Exception as e:  # noqa: BLE001 - listing is supplemental to stored ID
        if ruleset_id is None:
            raise
        log.warning(
            "enforcement_supplemental_scan_failed",
            extra={
                "owner": owner, "repo": repo,
                "install_id": install_id, "repo_id": repo_id,
                "stored_ruleset_id": ruleset_id,
                "kind": type(e).__name__,
                "detail": str(e)[:200],
            },
        )
    to_delete = sorted(to_delete_ids)

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
