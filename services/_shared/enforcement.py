"""Enforcement lifecycle — create/delete Grug-managed rulesets.

Called from dispatcher.py (on installation) and installations.py (on
persona toggle). Functions take install_token directly — callers wrap
with with_install_token_retry.

Required contexts (grug#1202): the ruleset always requires Chief, plus the
check run of every persona that is at BLOCK on the repo and is marked
`may_be_required` in the persona registry. `ensure_enforcement` applies that
on create and on every pass over an existing Grug-managed ruleset;
`reconcile_required_checks` is the settings-save entry point, which only
edits an existing Grug ruleset and never creates one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from github_rulesets_client import (
    EnforcementState,
    create_ruleset,
    delete_ruleset,
    detect_enforcement,
    get_ruleset,
    list_rulesets,
    repo_is_empty,
    update_ruleset,
)

from personas.registry import REGISTRY
from personas.tribe import (
    CHECK_CHIEF,
    LEGACY_RULESET_CHIEF,
    RULESET_CHIEF,
    is_enforcement_ruleset_name,
    primary_check_name,
)

log = logging.getLogger("grug.enforcement")

# Canonical tribe names (personas.tribe). Legacy aliases re-exported so
# existing imports keep working during the cutover.
GRUG_TPM_RULESET_NAME = RULESET_CHIEF
GRUG_DOR_CHECK_NAME = CHECK_CHIEF
# Back-compat spellings for scripts / tests that imported the old literals.
LEGACY_TPM_RULESET_NAME = LEGACY_RULESET_CHIEF

# The persona contexts enforcement adds and removes. Anything else on the
# ruleset - Chief, a non-Grug CI check, or a non-requirable persona an admin
# required by hand - is never touched by the persona reconcile.
_MANAGED_PERSONA_CHECKS: frozenset[str] = frozenset(
    spec.check_run_name for spec in REGISTRY if spec.may_be_required
)


def required_persona_checks(cfg: Mapping[str, Any]) -> list[str]:
    """Check-run names to require for this repo's persona modes, in registry
    order: every `may_be_required` persona that is enabled AND blocking.

    An unset flag falls back to the registry default, the same rule the
    dispatcher applies when it resolves a persona's mode."""
    out: list[str] = []
    for spec in REGISTRY:
        if not spec.may_be_required or spec.blocking_flag is None:
            continue
        enabled = bool(cfg.get(spec.enabled_flag, spec.enabled_default))
        blocking = bool(cfg.get(spec.blocking_flag, spec.blocking_default))
        if enabled and blocking:
            out.append(spec.check_run_name)
    return out


def _with_checks(rule: dict, checks: list[dict]) -> dict:
    """Copy of a required_status_checks rule with its contexts replaced and
    every other parameter kept. A null integration_id is dropped from each
    entry because GitHub 422s the whole PUT on it (same rule as
    migrate_check_context)."""
    cleaned = [
        {k: v for k, v in c.items() if not (k == "integration_id" and v is None)}
        for c in checks
    ]
    return {
        **rule,
        "parameters": {**rule.get("parameters", {}), "required_status_checks": cleaned},
    }


def _canonical(check: dict) -> str | None:
    ctx = check.get("context")
    return primary_check_name(ctx) if isinstance(ctx, str) else None


def _drop_unwanted(
    rules: list[dict], wanted: frozenset[str],
) -> tuple[list[dict], set[str], int | None, bool]:
    """Remove managed persona contexts that are not wanted. Returns the new
    rules, the managed contexts still present, the index of the first rule
    that requires Chief (where a missing persona context goes), and whether
    anything was removed."""
    new_rules: list[dict] = []
    present: set[str] = set()
    chief_rule: int | None = None
    changed = False
    for rule in rules:
        if rule.get("type") != "required_status_checks":
            new_rules.append(rule)
            continue
        checks = rule.get("parameters", {}).get("required_status_checks", [])
        kept = [
            c for c in checks
            if _canonical(c) not in _MANAGED_PERSONA_CHECKS or _canonical(c) in wanted
        ]
        present.update(
            name for c in kept
            if (name := _canonical(c)) in _MANAGED_PERSONA_CHECKS
        )
        if chief_rule is None and any(_canonical(c) == CHECK_CHIEF for c in kept):
            chief_rule = len(new_rules)
        if len(kept) != len(checks):
            changed = True
            new_rules.append(_with_checks(rule, kept))
        else:
            new_rules.append(rule)
    return new_rules, present, chief_rule, changed


def reconcile_persona_contexts(
    install_token: str,
    owner: str,
    repo: str,
    ruleset_id: int,
    wanted_persona_checks: list[str],
) -> bool:
    """Make a Grug-managed ruleset require exactly the wanted persona checks
    out of the managed set (grug#1202). Returns True if it PUT an update.

    Same discipline as migrate_check_context: fetch the ruleset, change only
    the managed persona contexts, and send the FULL rules array back so
    every other rule and every other context passes through unchanged. A
    legacy alias of a managed check counts as that check, so a ruleset that
    already names it (the hand-added interim edit, possibly scoped by an
    integration_id) gets no PUT and no duplicate. A missing context joins
    the rule that requires Chief; with no such rule there is nowhere safe
    to put it, so it is logged and skipped rather than inventing a rule."""
    wanted = frozenset(wanted_persona_checks) & _MANAGED_PERSONA_CHECKS
    ruleset = get_ruleset(install_token, owner, repo, ruleset_id)
    new_rules, present, chief_rule, changed = _drop_unwanted(
        ruleset.get("rules", []), wanted,
    )
    missing = [c for c in wanted_persona_checks if c in wanted and c not in present]
    if missing and chief_rule is None:
        log.warning(
            "enforcement_persona_context_no_chief_rule",
            extra={"owner": owner, "repo": repo, "ruleset_id": ruleset_id,
                   "missing": missing},
        )
    elif missing:
        host = new_rules[chief_rule]
        checks = list(host.get("parameters", {}).get("required_status_checks", []))
        new_rules[chief_rule] = _with_checks(
            host, checks + [{"context": c} for c in missing],
        )
        changed = True
    if not changed:
        return False
    update_ruleset(install_token, owner, repo, ruleset_id, new_rules)
    log.info(
        "enforcement_persona_contexts_reconciled",
        extra={"owner": owner, "repo": repo, "ruleset_id": ruleset_id,
               "required_personas": sorted(wanted)},
    )
    return True


def migrate_check_context(
    install_token: str,
    owner: str,
    repo: str,
    ruleset_id: int,
) -> bool:
    """Heal a Grug-managed ruleset whose required_status_checks context is
    a stale legacy alias (e.g. a pre-rename em-dash title) instead of the
    current canonical check name.

    Historically, a check-name rename (Chief/Hunt Plan cutover, and any
    future one) shipped a dual-post insurance mirror in github_checks_client
    so old rulesets kept passing - but nothing updated the ruleset's OWN
    required-check context to the new canonical name, so an already-enrolled
    repo stayed pinned to the old title forever (and, for the earliest
    em-dash titles, to a non-ASCII check name visible in the GitHub UI).
    The dual-post mirror itself was retired (grug#687) once fleet
    verification confirmed no ruleset anywhere still needed it; this
    healer remains as the actual fix for a ruleset's OWN stale context,
    for any Grug-managed ruleset ensure_enforcement still touches.
    Returns True if the ruleset was updated, False if it already names
    the canonical check.

    Rewrites ONLY known legacy aliases of a Grug persona check (Chief,
    Elder, Guard, ...) to that check's canonical name; every other
    required context is preserved unchanged (LORE on #685: an earlier
    version replaced the whole checks list with a Chief-only singleton,
    which would have silently dropped any other required check a ruleset
    carries). Also dedupes so a ruleset that somehow ended up with both
    the canonical name and a stale alias collapses to one entry. Inspects
    every required_status_checks rule on the ruleset, not just the first
    (LORE #685: GitHub does not document a one-rule-per-type limit).

    Sends update_ruleset the ruleset's FULL `rules` array with only the
    matching required_status_checks rule(s)' contexts changed - every
    other rule (and that rule's other parameters, e.g.
    strict_required_status_checks_policy) passes through byte-for-byte
    (FLINT #685: PUT /rulesets/{id} is not a documented partial-update
    endpoint, so a body built from a synthesized single rule risks
    silently dropping any OTHER rule type an admin added to the same
    ruleset).

    Within required_status_checks itself, an entry whose context is
    already canonical (or isn't a known legacy alias at all) is kept
    byte-for-byte, EXCEPT a null `integration_id` is always dropped
    (LORE #685: GitHub 422s the whole PUT on integration_id: null,
    including on an untouched entry re-sent verbatim - null and absent
    mean the same thing to GitHub's model, so this never changes what the
    entry actually requires). A rewritten entry keeps a real
    (non-null) `integration_id` unchanged (FLINT #685: an earlier
    version rebuilt every entry as bare {"context": ...}, silently
    dropping that scoping and collapsing same-named checks from different
    integrations). A non-string context is left alone entirely, not
    passed to primary_check_name (LORE #685: a malformed entry must not
    make the healed PUT itself malformed).
    """
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

    Defers (creates nothing) on a repo with no commits: a required check on
    an empty repo deadlocks the first push. The pull_request handler re-runs
    this on the first PR, when the repo necessarily has commits.

    Returns the resulting enforcement state after the operation.
    """
    from adapters.install_store import get_enforcement_id  # type: ignore
    stored_ruleset_id = get_enforcement_id(install_id, repo_id)
    detection = detect_enforcement(
        install_token, owner, repo, default_branch, GRUG_DOR_CHECK_NAME,
        stored_ruleset_id=stored_ruleset_id,
    )
    state = detection.state
    if state == "permission_denied":
        # grug#1001: the App cannot read or write rulesets here, so a create
        # would only 403 again. This is not "already enforced" either.
        log.warning(
            "enforcement_permission_denied",
            extra={"owner": owner, "repo": repo,
                   "install_id": install_id, "repo_id": repo_id},
        )
        from observability import emit_enforcement_metric  # type: ignore
        emit_enforcement_metric(f"{owner}/{repo}", state)
        return state
    if state != "none":
        if state == "grug_managed":
            # #686: heal the ruleset detection ACTUALLY matched, not the one
            # on file. `detect_enforcement` can classify grug_managed via the
            # name-prefix heuristic on a ruleset whose id differs from the
            # stored one (a rename or other drift - grug#565). Healing the
            # stale stored id calls get_ruleset(stale), 404s, gets swallowed
            # by the heal's broad except, and the repo silently never heals.
            ruleset_id = detection.ruleset_id or stored_ruleset_id
            if ruleset_id is not None and ruleset_id != stored_ruleset_id:
                # Persist the correction so the NEXT pass starts from truth -
                # same posture as heal_enforcement's post-recreate persist.
                # Best-effort: a store hiccup must not block the heal itself.
                try:
                    from adapters.install_store import set_enforcement_id  # type: ignore
                    set_enforcement_id(install_id, repo_id, ruleset_id)
                    log.info(
                        "enforcement_stored_ruleset_id_corrected",
                        extra={
                            "owner": owner, "repo": repo,
                            "install_id": install_id, "repo_id": repo_id,
                            "stored_ruleset_id": stored_ruleset_id,
                            "live_ruleset_id": ruleset_id,
                        },
                    )
                except Exception as e:  # noqa: BLE001 - persist is best-effort
                    log.warning(
                        "enforcement_stored_ruleset_id_persist_failed",
                        extra={
                            "owner": owner, "repo": repo,
                            "install_id": install_id, "repo_id": repo_id,
                            "kind": type(e).__name__,
                        },
                    )
            if ruleset_id is not None:
                _heal_managed_ruleset(
                    install_token, owner, repo, install_id, repo_id, ruleset_id,
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

    try:
        empty = repo_is_empty(install_token, owner, repo)
    except Exception as e:  # noqa: BLE001 - a hiccup here must not block creation
        log.warning(
            "enforcement_repo_is_empty_check_failed",
            extra={
                "owner": owner, "repo": repo,
                "install_id": install_id, "repo_id": repo_id,
                "kind": type(e).__name__,
            },
        )
        empty = False
    if empty:
        # Empty-repo bootstrap: creating a ruleset that requires Grug - Chief
        # on a repo with no commits deadlocks it. The required check can never
        # be satisfied - no PR can exist before the first commit lands - and
        # the push that would create the first commit is rejected by the
        # unsatisfied required check. So defer: create nothing, and let the
        # pull_request handler re-run ensure_enforcement on the first PR,
        # when the repo necessarily has commits. Returns "none" (it IS
        # unenforced); the enforcement-gap monitor excludes empty repos from
        # its denominator for the same reason it excludes archived ones.
        log.info(
            "enforcement_deferred_empty_repo",
            extra={
                "owner": owner, "repo": repo,
                "install_id": install_id, "repo_id": repo_id,
            },
        )
        from observability import emit_enforcement_metric  # type: ignore
        emit_enforcement_metric(f"{owner}/{repo}", "none")
        return "none"

    from adapters.install_store import get_repo_config  # type: ignore
    result = create_ruleset(
        install_token, owner, repo,
        GRUG_TPM_RULESET_NAME,
        [GRUG_DOR_CHECK_NAME, *required_persona_checks(get_repo_config(install_id, repo_id))],
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


def _heal_managed_ruleset(
    install_token: str,
    owner: str,
    repo: str,
    install_id: int,
    repo_id: int,
    ruleset_id: int,
) -> None:
    """Self-heal an existing Grug-managed ruleset: canonicalize stale legacy
    check aliases, then bring the persona contexts in line with the repo's
    modes (grug#1202). Each step is best-effort and isolated: a failure is
    logged and never blocks ensure_enforcement's existing-state return."""
    ids = {"owner": owner, "repo": repo, "install_id": install_id,
           "repo_id": repo_id, "ruleset_id": ruleset_id}
    try:
        if migrate_check_context(install_token, owner, repo, ruleset_id):
            log.info("enforcement_check_context_healed", extra=ids)
    except Exception as e:  # noqa: BLE001 - self-heal never blocks the existing-state return
        log.warning(
            "enforcement_check_context_heal_failed",
            extra={**ids, "kind": type(e).__name__, "detail": str(e)[:200]},
        )
    try:
        from adapters.install_store import get_repo_config  # type: ignore
        wanted = required_persona_checks(get_repo_config(install_id, repo_id))
        reconcile_persona_contexts(install_token, owner, repo, ruleset_id, wanted)
    except Exception as e:  # noqa: BLE001 - self-heal never blocks the existing-state return
        log.warning(
            "enforcement_persona_contexts_reconcile_failed",
            extra={**ids, "kind": type(e).__name__, "detail": str(e)[:200]},
        )


def reconcile_required_checks(
    install_token: str,
    owner: str,
    repo: str,
    default_branch: str,
    install_id: int,
    repo_id: int,
) -> EnforcementState:
    """Settings-save entry point (grug#1202): after a persona mode flip,
    bring the repo's EXISTING Grug-managed ruleset's required persona
    contexts in line with the stored modes. Returns the detected state.

    Unlike ensure_enforcement this never creates a ruleset - creating one
    is the tpm toggle's and the Fix button's job, and
    force_disable_enforcement exists to stop exactly that - and it never
    edits an external ruleset. Errors propagate so the caller can log them;
    the flag is already stored, and the next ensure pass reconciles again."""
    from adapters.install_store import get_enforcement_id, get_repo_config  # type: ignore
    stored_ruleset_id = get_enforcement_id(install_id, repo_id)
    detection = detect_enforcement(
        install_token, owner, repo, default_branch, GRUG_DOR_CHECK_NAME,
        stored_ruleset_id=stored_ruleset_id,
    )
    ruleset_id = detection.ruleset_id or stored_ruleset_id
    if detection.state != "grug_managed" or ruleset_id is None:
        log.info(
            "enforcement_reconcile_skipped",
            extra={"owner": owner, "repo": repo, "install_id": install_id,
                   "repo_id": repo_id, "state": detection.state},
        )
        return detection.state
    wanted = required_persona_checks(get_repo_config(install_id, repo_id))
    reconcile_persona_contexts(install_token, owner, repo, ruleset_id, wanted)
    return detection.state


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
