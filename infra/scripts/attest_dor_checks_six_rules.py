#!/usr/bin/env python3
"""Grounding attester for the DorCheck contract.

Proves four bools against the real source files:

  - `six_rules_exactly_no_more_no_less_per_process_gate_concepts`
  - `name_field_is_one_of_six_canonical_rule_names_per_process_gate_concepts`
  - `rule_function_returns_check_result_not_class_method_per_process_gate_concepts`
  - `check_result_is_frozen_dataclass_per_process_gate_concepts`

Asserts that the single shared `services/_shared/personas/tpm/dor_checks.py`
defines exactly the 7 canonical `check_*` functions (the file name predates the seventh, grug#1034) matching the rule
names in CONTEXT.md section "Process-gate concepts" AND that the
`CheckResult` dataclass is declared with `frozen=True` (peer-review HIGH
found a frozen=False regression that the DorCheck attester falsely passed).
Exits 1 on any drift.

Wired into .github/workflows/check.attesters.yml as one step of the
static-invariant pass.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

CANONICAL_RULES: frozenset[str] = frozenset(
    {"check_why", "check_acceptance", "check_estimate", "check_scope_fence", "check_issue_link", "check_linked_issue_completeness",
     # grug#1034: every ticket the PR names is an epic or belongs to one.
     "check_linked_issue_in_epic"}
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
                f"  DorCheck declares exactly {len(CANONICAL_RULES)} canonical rules; drift breaks the contract."
            )
        if not _check_result_is_frozen(path):
            failures.append(
                f"FAIL: {path}: CheckResult is not @dataclass(frozen=True). "
                f"The DorCheck contract attests `check_result_is_frozen_dataclass`; a bare @dataclass "
                f"lets `evaluation.results[0].passed = False` mutate the rollup silently."
            )
    if failures:
        print("\n".join(failures))
        return 1
    print(f"OK: dor_checks.py defines exactly {sorted(CANONICAL_RULES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
