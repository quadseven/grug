#!/usr/bin/env python3
"""Grounding attester for spec 0006.DorCheck.

Proves the bool:

  - `dor_checks_byte_identical_modulo_first_line_per_cross_service_primitives`

Per ADR-0001 (mirror-with-rule-of-three-deferral), modules in
CONTEXT.md § "Cross-service primitives (mirrored)" are byte-identical
between services/api/ and services/webhook/, **except** for the
mandatory `# MIRRORED — sibling at <path>; keep in lockstep ...` header
on line 1 (commit 641eba5), which by construction names the OTHER
sibling's path. We therefore compare bodies modulo the first line.

If a second-line divergence appears (logger namespace, etc.), the
spec author must either (a) annotate the file as intentionally
divergent and exclude it here, or (b) unify the implementations.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Pairs of (api_path, webhook_path) that the spec layer claims are mirrored
# modulo their first-line MIRRORED header. Extend as more mirrored modules
# graduate from "implicit" into spec coverage.
MIRRORED_PAIRS: tuple[tuple[Path, Path], ...] = (
    (
        REPO_ROOT / "services/api/personas/tpm/dor_checks.py",
        REPO_ROOT / "services/webhook/personas/tpm/dor_checks.py",
    ),
)

# First-line MIRRORED header pattern that legitimately diverges between
# siblings. Drop this line before hashing; anything else differing is
# a real drift bug.
_MIRRORED_HEADER_RE = re.compile(r"^# MIRRORED — sibling at .+; keep in lockstep\..*$")


def _body_sha512(path: Path) -> str:
    """SHA-512 over file bytes with the first line stripped if it matches
    the MIRRORED header pattern. Line endings are normalized to LF so
    Windows vs. unix checkouts don't false-alarm."""
    text = path.read_text()
    first_nl = text.find("\n")
    if first_nl == -1:
        body = text
    else:
        first_line = text[:first_nl]
        if _MIRRORED_HEADER_RE.match(first_line):
            body = text[first_nl + 1:]
        else:
            # No MIRRORED header — hash the whole file (caller's invariant
            # is "byte-identical" without an exception they didn't claim).
            body = text
    body = body.replace("\r\n", "\n")
    return hashlib.sha512(body.encode("utf-8")).hexdigest()


def main() -> int:
    # Vacuous-pass guard: zero pairs configured = "OK 0 verified" is a lie.
    # Peer-review HIGH — refuse to pass on an empty target set.
    if not MIRRORED_PAIRS:
        print("FAIL: MIRRORED_PAIRS is empty — refusing to pass vacuously")
        return 1
    failures: list[str] = []
    for api_path, webhook_path in MIRRORED_PAIRS:
        if not api_path.exists():
            failures.append(f"FAIL: {api_path} missing")
            continue
        if not webhook_path.exists():
            failures.append(f"FAIL: {webhook_path} missing")
            continue
        api_hash = _body_sha512(api_path)
        webhook_hash = _body_sha512(webhook_path)
        if api_hash != webhook_hash:
            failures.append(
                f"FAIL: body-mismatch between mirrored modules (after stripping line-1 MIRRORED header)\n"
                f"  {api_path}     sha512={api_hash[:16]}…\n"
                f"  {webhook_path} sha512={webhook_hash[:16]}…\n"
                f"  Per ADR-0001, these MUST be byte-identical modulo their line-1 MIRRORED header.\n"
                f"  Run `diff -u {api_path} {webhook_path}` to see the drift."
            )
    if failures:
        print("\n".join(failures))
        return 1
    print(f"OK: {len(MIRRORED_PAIRS)} mirrored module pair(s) verified body-identical (modulo line-1 MIRRORED header)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
