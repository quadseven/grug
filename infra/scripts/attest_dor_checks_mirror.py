#!/usr/bin/env python3
"""Grounding attester for spec 0006.DorCheck - single-copy dor_checks.

Proves the bool:

  - `dor_checks_byte_identical_modulo_first_line_per_cross_service_primitives`

Pre-#77 this compared the api/webhook mirror copies of dor_checks.py
modulo their line-1 MIRRORED headers (ADR-0001). Post-extraction
(ADR-0014) the mirror is gone: `personas/tpm/dor_checks.py` exists
EXACTLY ONCE, under services/_shared/. A single copy satisfies the bool's
byte-identity claim by construction (there is nothing left to diverge);
this attester proves the strictly stronger single-copy fact:

  1. services/_shared/personas/tpm/dor_checks.py exists.
  2. NEITHER service tree carries its own copy (a stray copy would shadow
     the shared one on sys.path and resurrect silent divergence).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_COPY = REPO_ROOT / "services/_shared/personas/tpm/dor_checks.py"
FORBIDDEN_COPIES = (
    REPO_ROOT / "services/api/personas/tpm/dor_checks.py",
    REPO_ROOT / "services/webhook/personas/tpm/dor_checks.py",
)


def main() -> int:
    failures: list[str] = []
    if not SHARED_COPY.exists():
        failures.append(f"FAIL: {SHARED_COPY.relative_to(REPO_ROOT)} missing")
    for copy in FORBIDDEN_COPIES:
        if copy.exists():
            failures.append(
                f"FAIL: {copy.relative_to(REPO_ROOT)} exists - it SHADOWS the "
                "shared copy on sys.path (ADR-0014: dor_checks.py lives ONCE, "
                "in services/_shared/)"
            )
    if failures:
        print("\n".join(failures))
        return 1
    print("OK: dor_checks.py single-copy invariant holds (services/_shared/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
