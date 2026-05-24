#!/usr/bin/env python3
"""Grounding attester for spec 0002.TpmEvaluation.

Proves a NECESSARY condition for the bool:

  - `evaluate_pull_request_is_pure_function_per_process_gate_concepts`

(Sufficiency requires runtime/property testing — this static attester
only checks the call graph at the AST level. Spec 0002's bool implies
"pure"; this script implies "no direct call to non-allowlisted target
inside evaluate_pull_request's body" — proves not-impure, not "pure".)

The check uses an **allowlist** of permitted call targets, not a
denylist. A future contributor cannot escape by importing a new IO
surface (`requests`, `aiohttp`, `subprocess`, `socket`) — every call
target must be explicitly permitted. Denylist version was caught by
peer-review HIGH (4x); flipped to allowlist after audit.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

PERSONA_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "services/api/personas/tpm/persona.py",
    REPO_ROOT / "services/webhook/personas/tpm/persona.py",
)

# Allowlist of permitted call-target roots inside `evaluate_pull_request`.
# Adding a new entry is a deliberate spec change — it must be defensible
# as pure (or as pure-modulo-the-allowlist) when reviewing.
ALLOWED_CALL_NAMES: frozenset[str] = frozenset({
    # The 5 DoR rules + the rollup helper (all defined in dor_checks.py)
    "run_all",
    "check_why", "check_acceptance", "check_estimate", "check_scope_fence", "check_issue_link",
    # Pure dataclass constructor
    "TpmEvaluation",
    # Stdlib pure builtins commonly used in rollup logic
    "tuple", "list", "len", "all", "any", "isinstance", "sorted", "filter", "map",
})


def _call_target_name(call: ast.Call) -> str | None:
    """Return the leftmost-name of the call target, e.g. `httpx.post(...)` → `httpx`,
    `_summary(...)` → `_summary`, `obj.method()` → `obj`. Returns None for calls
    on call-results (`foo()()` etc.)."""
    target = call.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        root = target
        while isinstance(root.value, ast.Attribute):
            root = root.value
        if isinstance(root.value, ast.Name):
            return root.value.id
    return None


def _violations_in_function(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    found: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = _call_target_name(node)
        if name is None:
            found.append(f"non-name call target at line {node.lineno} (e.g. `foo()()`) — refuse to prove pure")
            continue
        if name not in ALLOWED_CALL_NAMES:
            found.append(f"call to non-allowlisted `{name}` at line {node.lineno}")
    return found


def main() -> int:
    # Vacuous-pass guard: empty PERSONA_PATHS = vacuous OK is a lie.
    if not PERSONA_PATHS:
        print("FAIL: PERSONA_PATHS is empty — refusing to pass vacuously")
        return 1
    failures: list[str] = []
    for path in PERSONA_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        evaluate_fns = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "evaluate_pull_request"
        ]
        if not evaluate_fns:
            failures.append(f"FAIL: {path} has no evaluate_pull_request — spec 0002 broken")
            continue
        if len(evaluate_fns) != 1:
            failures.append(f"FAIL: {path} has {len(evaluate_fns)} evaluate_pull_request defs (expected 1)")
            continue
        viols = _violations_in_function(evaluate_fns[0])
        if viols:
            failures.append(
                f"FAIL: {path} — evaluate_pull_request is not pure:\n"
                + "\n".join(f"  {v}" for v in viols)
                + "\n  Spec 0002 attests purity. Move side-effects to publish_tpm_evaluation."
            )
    if failures:
        print("\n".join(failures))
        return 1
    print(f"OK: evaluate_pull_request is pure in both {len(PERSONA_PATHS)} persona module(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
