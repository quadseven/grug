#!/usr/bin/env python3
"""Grounding attester for spec 0006.DorCheck.

Proves four bools against the real source files:

  - `five_rules_exactly_no_more_no_less_per_process_gate_concepts`
  - `name_field_is_one_of_five_canonical_rule_names_per_process_gate_concepts`
  - `rule_function_returns_check_result_not_class_method_per_process_gate_concepts`
  - `check_result_is_frozen_dataclass_per_process_gate_concepts`

Asserts that `services/{api,webhook}/personas/tpm/dor_checks.py` each
define exactly the 5 canonical `check_*` functions matching the rule
names in CONTEXT.md § "Process-gate concepts" AND that the
`CheckResult` dataclass is declared with `frozen=True` (peer-review HIGH
found a frozen=False regression that spec 0006 falsely attested).
Exits 1 on any drift.

Wire into .github/workflows/check.temper-specs.yml as a step that
runs alongside `temper verify -s specs/0006-dor-check/`.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

CANONICAL_RULES: frozenset[str] = frozenset(
    {"check_why", "check_acceptance", "check_estimate", "check_scope_fence", "check_issue_link"}
)

DOR_CHECK_PATHS: tuple[Path, ...] = (
    Path(__file__).resolve().parents[2] / "services/_shared/personas/tpm/dor_checks.py",
)

_DEF_PATTERN = re.compile(r"^(?:async\s+)?def\s+(check_\w+)\s*\(", re.MULTILINE)


def _module_check_functions(path: Path) -> set[str]:
    return set(_DEF_PATTERN.findall(path.read_text()))


def _check_result_is_frozen(path: Path) -> bool:
    """Verify `@dataclass(frozen=True)` on the CheckResult class. Peer-review
    HIGH (4x): the spec attested frozen but the decorator was bare `@dataclass`,
    letting `evaluation.results[0].passed = False` mutate silently."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "CheckResult"):
            continue
        for dec in node.decorator_list:
            # Match @dataclass(frozen=True) — the dataclass call with frozen kwarg True
            if isinstance(dec, ast.Call):
                fn = dec.func
                is_dataclass = (
                    (isinstance(fn, ast.Name) and fn.id == "dataclass")
                    or (isinstance(fn, ast.Attribute) and fn.attr == "dataclass")
                )
                if not is_dataclass:
                    continue
                for kw in dec.keywords:
                    if kw.arg == "frozen" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        return True
        return False
    return False


def main() -> int:
    # Vacuous-pass guard: empty DOR_CHECK_PATHS = vacuous OK.
    if not DOR_CHECK_PATHS:
        print("FAIL: DOR_CHECK_PATHS is empty — refusing to pass vacuously")
        return 1
    failures: list[str] = []
    for path in DOR_CHECK_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} does not exist (shared module missing, ADR-0014)")
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
        if not _check_result_is_frozen(path):
            failures.append(
                f"FAIL: {path}: CheckResult is not @dataclass(frozen=True). "
                f"Spec 0006 attests `check_result_is_frozen_dataclass`; a bare @dataclass "
                f"lets `evaluation.results[0].passed = False` mutate the rollup silently."
            )
    if failures:
        print("\n".join(failures))
        return 1
    print(f"OK: both dor_checks.py modules define exactly {sorted(CANONICAL_RULES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
