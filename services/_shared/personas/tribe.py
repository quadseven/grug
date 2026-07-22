"""Tribe nomenclature - one source of truth for caveman product names.

ADR-0002: canonical identity is the caveman name (chief, elder, ...).
GitHub check-run titles use ``Grug - <Caveman>`` (ASCII hyphen) so the
checks UI matches the dashboard, Activity feed, and PR comments.

Historical titles (em-dash ``Grug - `` variants and pre-polish names)
remain accepted for ruleset detection and healing (grug#687: the
check-run dual-post mirror itself was retired once fleet verification
confirmed no ruleset anywhere still names a legacy context) - only
canonical titles are posted now.
This module is plain ASCII source; em-dash aliases use \\u escapes.
"""

from __future__ import annotations

# --- Check-run titles (posted to GitHub) - ASCII only -----------------------

CHECK_CHIEF = "Grug - Chief"
CHECK_ELDER = "Grug - Elder"
CHECK_GUARD = "Grug - Guard"
CHECK_WARDER = "Grug - Warder"
CHECK_SMASHER = "Grug - Smasher"
CHECK_TELLER = "Grug - Teller"
CHECK_PULSE = "Grug - Pulse"
CHECK_SENTINEL = "Grug - Sentinel"

# Pre-polish / em-dash titles still live on some rulesets and old check-runs.
_EM = "\u2014"  # historical em dash used in early "Grug - X" titles
LEGACY_CHECK_CHIEF = "Grug - Definition of Ready"
LEGACY_CHECK_ELDER = "Grug - Code Review"
LEGACY_CHECK_CHIEF_EM = f"Grug {_EM} Definition of Ready"
LEGACY_CHECK_ELDER_EM = f"Grug {_EM} Code Review"
LEGACY_CHECK_CHIEF_EM_SHORT = f"Grug {_EM} Chief"
LEGACY_CHECK_ELDER_EM_SHORT = f"Grug {_EM} Elder"
LEGACY_CHECK_GUARD_EM = f"Grug {_EM} Guard"
LEGACY_CHECK_WARDER_EM = f"Grug {_EM} Warder"
LEGACY_CHECK_SMASHER_EM = f"Grug {_EM} Smasher"
LEGACY_CHECK_TELLER_EM = f"Grug {_EM} Teller"
LEGACY_CHECK_PULSE_EM = f"Grug {_EM} Pulse"

# primary -> aliases that mean the same gate (detection + healing only -
# post_check_run no longer dual-posts these, grug#687)
_CHECK_ALIASES: dict[str, tuple[str, ...]] = {
    CHECK_CHIEF: (
        LEGACY_CHECK_CHIEF,
        LEGACY_CHECK_CHIEF_EM,
        LEGACY_CHECK_CHIEF_EM_SHORT,
    ),
    CHECK_ELDER: (
        LEGACY_CHECK_ELDER,
        LEGACY_CHECK_ELDER_EM,
        LEGACY_CHECK_ELDER_EM_SHORT,
    ),
    CHECK_GUARD: (LEGACY_CHECK_GUARD_EM,),
    CHECK_WARDER: (LEGACY_CHECK_WARDER_EM,),
    CHECK_SMASHER: (LEGACY_CHECK_SMASHER_EM,),
    CHECK_TELLER: (LEGACY_CHECK_TELLER_EM,),
    CHECK_PULSE: (LEGACY_CHECK_PULSE_EM,),
}

# alias -> primary (for reverse lookup when reading old rulesets)
_ALIAS_TO_PRIMARY: dict[str, str] = {
    alias: primary
    for primary, aliases in _CHECK_ALIASES.items()
    for alias in aliases
}

# --- Ruleset ownership names ------------------------------------------------

RULESET_CHIEF = "Grug - Chief Enforcement"
LEGACY_RULESET_CHIEF = "Grug - TPM Enforcement"
LEGACY_RULESET_CHIEF_EM = f"Grug {_EM} TPM Enforcement"
LEGACY_RULESET_CHIEF_EM_SHORT = f"Grug {_EM} Chief Enforcement"

# Every exact name grug's own Chief enforcement ruleset has ever carried
# (canonical + the TPM/em-dash legacies). Matching these EXACT names - not a
# broad "Grug - " prefix - is what keeps the no-stored-id delete fallback from
# removing an unrelated user ruleset that merely shares the prefix.
_ENFORCEMENT_RULESET_NAMES: frozenset[str] = frozenset({
    RULESET_CHIEF,
    LEGACY_RULESET_CHIEF,
    LEGACY_RULESET_CHIEF_EM,
    LEGACY_RULESET_CHIEF_EM_SHORT,
})

# --- Capability names (not full personas; product voice) --------------------

# Chief's gate on PR body shape (was industry jargon "Definition of Ready").
# Product surfaces say Hunt Plan; code still has dor_checks.py as the module.
HUNT_PLAN = "Hunt Plan"
# Exploitability filter for Guard/Elder findings (was "judge").
SEER = "Seer"
# Production-signal fusion into Elder (#470).
OMEN = "Omen"
# Prior-review ledger / precedent.
LORE = "Lore"
# Elder's structured findings surface.
MARKINGS = "Markings"
# Self-hosted inference fleet.
CAVE = "Cave"
# Quiet-window tiers for Elder settle.
SWIFT_HUNT = "Swift Hunt"
STEADY_HUNT = "Steady Hunt"
FULL_HUNT = "Full Hunt"
LIVING_HUNT = "Living Hunt"


def is_enforcement_ruleset_name(name: str) -> bool:
    """True if `name` is one of grug's own Chief enforcement ruleset names.

    Exact-match against the known set (canonical + legacies), NOT a prefix
    test: a user ruleset named e.g. "Grug - my rules" shares the ownership
    prefix but is not grug's enforcement and must never be deleted by the
    no-stored-id fallback."""
    return name in _ENFORCEMENT_RULESET_NAMES


def check_aliases(check_name: str) -> tuple[str, ...]:
    """Legacy alias titles for one canonical check name (may be empty)."""
    return _CHECK_ALIASES.get(check_name, ())


def acceptable_check_names(check_name: str) -> tuple[str, ...]:
    """Primary + aliases - any of these count as the same gate for detection."""
    return (check_name, *check_aliases(check_name))


def primary_check_name(name: str) -> str:
    """Map a possibly-legacy title to the current canonical title."""
    return _ALIAS_TO_PRIMARY.get(name, name)


def is_same_check(context: str, check_name: str) -> bool:
    """True if a ruleset/legacy context matches primary or its aliases."""
    return context in acceptable_check_names(check_name)
