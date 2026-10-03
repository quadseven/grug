#!/usr/bin/env python3
"""Attest that every backend repo-config flag is either rendered as a
dashboard control or explicitly listed in OPERATOR_ONLY with a reason (#768).

WHY THIS EXISTS
---------------
The dashboard advertised "toggle personas" but rendered only 2 toggles while
the backend exposed 14+ flags. Six personas could not be toggled at all, and
a shipped feature (reopen_watch) was unreachable because its flag had no
control. Worse, the incoherence produced a wrong answer to an operator.

This script closes the hole: it fails when a backend flag is silently absent
— neither wired to a UI control nor declared OPERATOR_ONLY. Add a flag to the
backend, and you must either surface it or state why it stays operator-only.

HOW IT WORKS
------------
1. Backend flags: parsed from the Pydantic model in services/api/installations.py
   (fields ending in `_enabled`, plus the non-boolean `elder_voice`).
2. UI flags: parsed from web/src/routes/Dashboard.tsx — flags passed to
   setConfig.mutate() in toggle handlers.
3. OPERATOR_ONLY: explicit set below with a reason per flag.

A flag in (1) but in neither (2) nor (3) fails the check.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Flags deliberately not surfaced in the dashboard UI, with the reason why.
# Every entry must have a non-empty reason — an unexplained omission is the
# exact failure this script exists to catch.
OPERATOR_ONLY: dict[str, str] = {
    # Persona master switches beyond the two the dashboard advertises.
    # Surfacing 8 persona toggles would clutter the repo row; the dashboard
    # is for the common case (Grug on/off, Guard on/off, watches). Operators
    # manage these via the API.
    "code_reviewer_enabled": "persona master switch; operator manages via API",
    "code_reviewer_blocking": "review-gating semantics; operator manages via API",
    "guard_blocking": "enforcement-gating semantics; operator manages via API",
    "warder_enabled": "persona master switch; operator manages via API",
    "warder_gate_blocking": "deploy-gate semantics; operator manages via API",
    "sentinel_enabled": "persona master switch; operator manages via API",
    "pulse_enabled": "persona master switch; operator manages via API",
    "pulse_comment_nudge_enabled": "persona master switch; operator manages via API",
    "smasher_enabled": "persona master switch; operator manages via API",
    "walkthrough_enabled": "persona master switch; operator manages via API",
    "issue_dor_enabled": "Chief issue-time advisory; operator manages via API",
    "check_run_reconcile_enabled": "periodic sweep opt-in; operator manages via API",
}


def backend_flags(repo: Path = REPO) -> set[str]:
    """Parse flag fields from the installations Pydantic model."""
    text = (repo / "services" / "api" / "installations.py").read_text()
    # Fields like `tpm_enabled: bool | None = Field(default=None)` or
    # `guard_blocking: bool | None = Field(default=None)`
    flags = set(re.findall(r"^\s{4}(\w+_(?:enabled|blocking))\s*:", text, re.MULTILINE))
    # Non-boolean config with entitlement rules (not a toggle, but must be declared)
    if re.search(r"^\s{4}elder_voice\s*:", text, re.MULTILINE):
        flags.add("elder_voice")
    return flags


def ui_flags(repo: Path = REPO) -> set[str]:
    """Parse flags wired to dashboard toggle handlers (setConfig.mutate)."""
    text = (repo / "web" / "src" / "routes" / "Dashboard.tsx").read_text()
    # setConfig.mutate({ repo_id: ..., tpm_enabled: enabled, ... })
    flags: set[str] = set()
    for m in re.finditer(r"setConfig\.mutate\(\{([^}]+)\}", text):
        flags.update(re.findall(r"(\w+_enabled)\s*:", m.group(1)))
    return flags


def find_silently_absent_flags(
    backend_flags: set[str],
    ui_flags: set[str],
    operator_only: dict[str, str],
) -> set[str]:
    """Flags in the backend model but in neither the UI nor OPERATOR_ONLY."""
    return backend_flags - ui_flags - set(operator_only)


def main() -> int:
    backend = backend_flags()
    ui = ui_flags()
    missing = find_silently_absent_flags(backend, ui, OPERATOR_ONLY)
    # OPERATOR_ONLY entries must all have reasons and must refer to real flags.
    bad_reasons = [k for k, v in OPERATOR_ONLY.items() if not v.strip()]
    unknown = set(OPERATOR_ONLY) - backend
    print(
        f"backend flags: {len(backend)}, ui flags: {len(ui)}, "
        f"operator-only: {len(OPERATOR_ONLY)}"
    )
    ok = True
    if missing:
        print(
            f"FAIL: silently absent flags (no UI control, not OPERATOR_ONLY): "
            f"{sorted(missing)}"
        )
        ok = False
    if bad_reasons:
        print(f"FAIL: OPERATOR_ONLY entries without a reason: {bad_reasons}")
        ok = False
    if unknown:
        print(f"FAIL: OPERATOR_ONLY entries not in backend model: {sorted(unknown)}")
        ok = False
    if ok:
        print("OK: every backend flag has a UI control or an OPERATOR_ONLY reason")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
