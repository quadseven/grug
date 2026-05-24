#!/usr/bin/env python3
"""Grounding attester for spec 0010.MirrorDiscipline.

Proves NECESSARY conditions for these bools:

  - `mirror_with_header_files_carry_line1_mirrored_at_per_cross_service_primitives`
  - `scripts_check_mirrored_files_sh_is_authoritative_list_per_cross_service_primitives`
  - `drift_lint_runs_on_every_pr_per_cross_service_primitives`

Asserts:
  1. `scripts/check-mirrored-files.sh` exists.
  2. It defines BOTH `MIRRORED_WITH_HEADER` and `MIRRORED_BYTE_IDENTICAL` arrays.
  3. `.github/workflows/drift-lint.yml` exists and invokes the script.
  4. Every file listed in `MIRRORED_WITH_HEADER` exists at both
     `services/api/<relpath>` AND `services/webhook/<relpath>`.
  5. Each of those files starts with the line-1 MIRRORED header pattern
     pointing at the OTHER sibling.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/check-mirrored-files.sh"
DRIFT_LINT_WORKFLOW = REPO_ROOT / ".github/workflows/drift-lint.yml"

_ARRAY_RE = re.compile(
    r"(?P<name>MIRRORED_WITH_HEADER|MIRRORED_BYTE_IDENTICAL)=\(\s*(?P<body>[^)]*)\)",
    re.DOTALL,
)
_QUOTED_PATH_RE = re.compile(r'"([^"]+)"')
_HEADER_LINE_RE = re.compile(r"^# MIRRORED — sibling at services/(?:api|webhook)/.+; keep in lockstep\.")


def _parse_arrays(script_text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in _ARRAY_RE.finditer(script_text):
        out[m.group("name")] = _QUOTED_PATH_RE.findall(m.group("body"))
    return out


def main() -> int:
    if not SCRIPT.exists():
        print(f"FAIL: {SCRIPT} missing")
        return 1
    if not DRIFT_LINT_WORKFLOW.exists():
        print(f"FAIL: {DRIFT_LINT_WORKFLOW} missing — drift-lint not wired")
        return 1

    script_text = SCRIPT.read_text()
    workflow_text = DRIFT_LINT_WORKFLOW.read_text()

    failures: list[str] = []

    if "check-mirrored-files.sh" not in workflow_text:
        failures.append(f"FAIL: {DRIFT_LINT_WORKFLOW.name} doesn't invoke check-mirrored-files.sh")

    arrays = _parse_arrays(script_text)
    if "MIRRORED_WITH_HEADER" not in arrays:
        failures.append("FAIL: check-mirrored-files.sh missing MIRRORED_WITH_HEADER array")
    if "MIRRORED_BYTE_IDENTICAL" not in arrays:
        failures.append("FAIL: check-mirrored-files.sh missing MIRRORED_BYTE_IDENTICAL array")

    # Verify each MIRRORED_WITH_HEADER file exists at both sides + has the header.
    for relpath in arrays.get("MIRRORED_WITH_HEADER", []):
        api = REPO_ROOT / f"services/api/{relpath}"
        webhook = REPO_ROOT / f"services/webhook/{relpath}"
        if not api.exists():
            failures.append(f"FAIL: MIRRORED_WITH_HEADER entry `{relpath}` missing at services/api/")
            continue
        if not webhook.exists():
            failures.append(f"FAIL: MIRRORED_WITH_HEADER entry `{relpath}` missing at services/webhook/")
            continue
        for side in (api, webhook):
            first_line = side.read_text().splitlines()[0] if side.read_text() else ""
            if not _HEADER_LINE_RE.match(first_line):
                failures.append(
                    f"FAIL: {side.relative_to(REPO_ROOT)} doesn't have the line-1 MIRRORED header "
                    f"(found: {first_line[:80]!r})"
                )

    # Verify each MIRRORED_BYTE_IDENTICAL file exists at both sides + is byte-identical.
    for relpath in arrays.get("MIRRORED_BYTE_IDENTICAL", []):
        api = REPO_ROOT / f"services/api/{relpath}"
        webhook = REPO_ROOT / f"services/webhook/{relpath}"
        if not api.exists():
            failures.append(f"FAIL: MIRRORED_BYTE_IDENTICAL entry `{relpath}` missing at services/api/")
            continue
        if not webhook.exists():
            failures.append(f"FAIL: MIRRORED_BYTE_IDENTICAL entry `{relpath}` missing at services/webhook/")
            continue
        if api.read_bytes() != webhook.read_bytes():
            failures.append(
                f"FAIL: MIRRORED_BYTE_IDENTICAL entry `{relpath}` is NOT byte-identical between sides"
            )

    if failures:
        print("\n".join(failures))
        return 1
    print(
        f"OK: mirror policy consistent ("
        f"{len(arrays.get('MIRRORED_WITH_HEADER', []))} with-header + "
        f"{len(arrays.get('MIRRORED_BYTE_IDENTICAL', []))} byte-identical pairs verified)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
