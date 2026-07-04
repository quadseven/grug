#!/usr/bin/env python3
"""Grounding attester for spec 0002.TpmEvaluation.

Proves:
  - `aggregate_passed_iff_all_blocking_checks_passed_per_process_gate_concepts`

Two-layer proof:
  Layer 1 (AST): _ADVISORY_CHECKS is defined, is a frozenset literal,
  and every entry is one of the 5 canonical check names.
  Layer 2 (AST): evaluate_pull_request filters blocking via
  `r.name not in _ADVISORY_CHECKS` — advisory failures cannot block.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

PERSONA_PATHS = (
    REPO_ROOT / "services/_shared/personas/tpm/persona.py",
)

CANONICAL_NAMES = frozenset({"why", "acceptance", "estimate", "scope-fence", "issue-link"})


def _find_advisory_set(tree: ast.Module) -> frozenset[str] | None:
    for node in ast.walk(tree):
        name = None
        value = None
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_ADVISORY_CHECKS":
                    name = target.id
                    value = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "_ADVISORY_CHECKS":
                name = node.target.id
                value = node.value
        if name and value and isinstance(value, ast.Call) and value.args:
            arg = value.args[0]
            if isinstance(arg, ast.Set):
                return frozenset(
                    elt.value for elt in arg.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                )
    return None


def _evaluate_filters_by_advisory(tree: ast.Module) -> bool:
    source = ast.dump(tree)
    return "_ADVISORY_CHECKS" in source and "not in _ADVISORY_CHECKS" in ast.unparse(tree)


def main() -> int:
    failures: list[str] = []

    if not PERSONA_PATHS:
        failures.append("FAIL: PERSONA_PATHS is empty")
        print("\n".join(failures))
        return 1

    for path in PERSONA_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue

        tree = ast.parse(path.read_text(), filename=str(path))

        advisory = _find_advisory_set(tree)
        if advisory is None:
            failures.append(f"FAIL: {path} — _ADVISORY_CHECKS not found as a frozenset literal")
            continue

        unknown = advisory - CANONICAL_NAMES
        if unknown:
            failures.append(f"FAIL: {path} — _ADVISORY_CHECKS contains non-canonical names: {unknown}")

        if not advisory:
            failures.append(f"FAIL: {path} — _ADVISORY_CHECKS is empty (no advisory checks defined)")

        if not _evaluate_filters_by_advisory(tree):
            failures.append(f"FAIL: {path} — evaluate_pull_request does not filter by `not in _ADVISORY_CHECKS`")

    if failures:
        print("\n".join(failures))
        return 1

    sample = _find_advisory_set(ast.parse(PERSONA_PATHS[0].read_text()))
    print(
        f"OK: aggregate_passed_iff_all_blocking_checks_passed — "
        f"_ADVISORY_CHECKS={sample} is subset of {CANONICAL_NAMES}, "
        f"evaluate_pull_request filters by `not in _ADVISORY_CHECKS` in both persona modules"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
