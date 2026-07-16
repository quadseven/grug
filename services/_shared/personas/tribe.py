"""Tribe nomenclature — one source of truth for caveman product names.

ADR-0002: canonical identity is the caveman name (chief, elder, …).
GitHub check-run titles use ``Grug — <Caveman>`` so the checks UI matches
the dashboard, Activity feed, and PR comments.

Historical (pre-tribe-polish) check names remain accepted for ruleset
detection and are dual-posted as thin alias status checks so existing
required-status rulesets keep working until operators re-point them.
"""

from __future__ import annotations

# --- Check-run titles (posted to GitHub) ------------------------------------

CHECK_CHIEF = "Grug — Chief"
CHECK_ELDER = "Grug — Elder"
CHECK_GUARD = "Grug — Guard"
CHECK_WARDER = "Grug — Warder"
CHECK_SMASHER = "Grug — Smasher"
CHECK_TELLER = "Grug — Teller"
CHECK_PULSE = "Grug — Pulse"

# Pre-polish titles. Still accepted as "enforced" and dual-posted as alias
# mirrors so required-status rulesets do not brick merges mid-cutover.
LEGACY_CHECK_CHIEF = "Grug — Definition of Ready"
LEGACY_CHECK_ELDER = "Grug — Code Review"

# primary -> aliases that mean the same gate
_CHECK_ALIASES: dict[str, tuple[str, ...]] = {
    CHECK_CHIEF: (LEGACY_CHECK_CHIEF,),
    CHECK_ELDER: (LEGACY_CHECK_ELDER,),
}

# alias -> primary (for reverse lookup when reading old rulesets)
_ALIAS_TO_PRIMARY: dict[str, str] = {
    alias: primary
    for primary, aliases in _CHECK_ALIASES.items()
    for alias in aliases
}

# --- Ruleset ownership names ------------------------------------------------

RULESET_CHIEF = "Grug — Chief Enforcement"
LEGACY_RULESET_CHIEF = "Grug — TPM Enforcement"

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


def check_aliases(check_name: str) -> tuple[str, ...]:
    """Legacy alias titles for one canonical check name (may be empty)."""
    return _CHECK_ALIASES.get(check_name, ())


def acceptable_check_names(check_name: str) -> tuple[str, ...]:
    """Primary + aliases — any of these count as the same gate for detection."""
    return (check_name, *check_aliases(check_name))


def primary_check_name(name: str) -> str:
    """Map a possibly-legacy title to the current canonical title."""
    return _ALIAS_TO_PRIMARY.get(name, name)


def is_same_check(context: str, check_name: str) -> bool:
    """True if a ruleset/legacy context matches primary or its aliases."""
    return context in acceptable_check_names(check_name)
