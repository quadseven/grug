#!/usr/bin/env python3
"""Grounding attester for spec 0010.MirrorDiscipline - EXTRACTED state.

The MirrorDiscipline automaton reached its terminal `Extracted` state at
#77 / ADR-0014: the rule-of-three fired (Smasher was the third async
persona, ADR-0013) and the mirrored modules graduated to services/_shared/.

Pre-extraction this script proved the Copied/Synced-state contracts
(headers, byte-identity, drift-lint wiring). Post-extraction it proves the
NECESSARY conditions of the Extracted state:

  1. `services/_shared/` exists and contains the shared import roots.
  2. NO SHADOWING: no relative path under `services/api/` or
     `services/webhook/` duplicates a path in `services/_shared/`. The
     service dir precedes _shared/ on sys.path, so a stray copy would
     silently shadow the shared module for that one service - the
     post-extraction drift class (the pytest twin lives in
     services/webhook/tests/test_shared_no_shadowing.py).
  3. The retired enforcement is GONE: scripts/check-mirrored-files.sh and
     .github/workflows/check.drift-lint.yml must not exist.
  4. No line-1 ADR-0001 `# MIRRORED — sibling at` headers remain under
     services/.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED = REPO_ROOT / "services/_shared"
SERVICE_TREES = (REPO_ROOT / "services/api", REPO_ROOT / "services/webhook")
RETIRED = (
    REPO_ROOT / "scripts/check-mirrored-files.sh",
    REPO_ROOT / ".github/workflows/check.drift-lint.yml",
)
# Anchor modules that must exist in _shared/ - guards against the attester
# passing vacuously if the tree were emptied or relocated.
EXPECTED_SHARED = (
    "observability.py",
    "secrets_loader.py",
    "adapters/install_store.py",
    "personas/registry.py",
    "personas/tpm/dor_checks.py",
    "ports/token_cache.py",
    "github_app_auth/__init__.py",
)


def _py_relpaths(root: Path) -> list[str]:
    return [
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def main() -> int:
    failures: list[str] = []

    if not SHARED.is_dir():
        print(f"FAIL: {SHARED.relative_to(REPO_ROOT)} missing - not Extracted")
        return 1

    for rel in EXPECTED_SHARED:
        if not (SHARED / rel).is_file():
            failures.append(f"FAIL: expected shared module missing: services/_shared/{rel}")

    shared_rels = set(_py_relpaths(SHARED))
    if len(shared_rels) < 40:
        failures.append(
            f"FAIL: services/_shared/ holds only {len(shared_rels)} modules - "
            "expected the full extracted set (>=40)"
        )

    for tree in SERVICE_TREES:
        for rel in _py_relpaths(tree):
            if rel in shared_rels:
                failures.append(
                    f"FAIL: {tree.relative_to(REPO_ROOT)}/{rel} SHADOWS "
                    f"services/_shared/{rel} - edit the shared copy instead (ADR-0014)"
                )

    for retired in RETIRED:
        if retired.exists():
            failures.append(
                f"FAIL: {retired.relative_to(REPO_ROOT)} still exists - "
                "the mirror enforcement was retired at #77"
            )

    for tree in (*SERVICE_TREES, SHARED):
        for p in tree.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            text = p.read_text()
            first = text.splitlines()[0] if text else ""
            if first.startswith("# MIRRORED — sibling at"):
                failures.append(
                    f"FAIL: {p.relative_to(REPO_ROOT)} carries an ADR-0001 MIRRORED "
                    "header - the convention died with the extraction (ADR-0014)"
                )

    if failures:
        print("\n".join(failures))
        return 1
    print(
        f"OK: Extracted state consistent ({len(shared_rels)} shared modules, "
        "zero shadowing, mirror enforcement retired)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
