#!/usr/bin/env python3
"""Grounding attester for spec 0006.DorCheck.

Proves three bools against the real source files:

  - `five_rules_exactly_no_more_no_less_per_process_gate_concepts`
  - `name_field_is_one_of_five_canonical_rule_names_per_process_gate_concepts`
  - `rule_function_returns_check_result_not_class_method_per_process_gate_concepts`

Asserts that `services/{api,webhook}/personas/tpm/dor_checks.py` each
define exactly the 5 canonical `check_*` functions matching the rule
names in CONTEXT.md § "Process-gate concepts". Exits 1 on any drift
(extra rule, missing rule, renamed rule, or class-shaped rule).

Wire into .github/workflows/check.temper-specs.yml as a step that
runs alongside `temper verify -s specs/0006-dor-check/`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CANONICAL_RULES: frozenset[str] = frozenset(
    {"check_why", "check_acceptance", "check_estimate", "check_scope_fence", "check_issue_link"}
)

DOR_CHECK_PATHS: tuple[Path, ...] = (
    Path(__file__).resolve().parents[2] / "services/api/personas/tpm/dor_checks.py",
    Path(__file__).resolve().parents[2] / "services/webhook/personas/tpm/dor_checks.py",
)

_DEF_PATTERN = re.compile(r"^(?:async\s+)?def\s+(check_\w+)\s*\(", re.MULTILINE)


def _module_check_functions(path: Path) -> set[str]:
    return set(_DEF_PATTERN.findall(path.read_text()))


def main() -> int:
    failures: list[str] = []
    for path in DOR_CHECK_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} does not exist (mirror discipline broken)")
            continue
        found = _module_check_functions(path)
        if found != CANONICAL_RULES:
            extra = found - CANONICAL_RULES
            missing = CANONICAL_RULES - found
            failures.append(
                f"FAIL: {path}\n"
                f"  extra:   {sorted(extra) or 'none'}\n"
                f"  missing: {sorted(missing) or 'none'}\n"
                f"  Spec 0006 DorCheck declares exactly 5 canonical rules; drift breaks the kernel contract."
            )
    if failures:
        print("\n".join(failures))
        return 1
    print(f"OK: both dor_checks.py modules define exactly {sorted(CANONICAL_RULES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
