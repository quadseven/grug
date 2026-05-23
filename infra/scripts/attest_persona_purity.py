#!/usr/bin/env python3
"""Grounding attester for spec 0002.TpmEvaluation.

Proves the bool:

  - `evaluate_pull_request_is_pure_function_per_process_gate_concepts`

Asserts that in both `services/{api,webhook}/personas/tpm/persona.py`,
the body of `evaluate_pull_request` does NOT call any of the known
side-effect surfaces (`with_install_token_retry`, `post_check_run`,
`log.*`, `httpx.*`, `boto3.*`). Those calls belong in
`publish_tpm_evaluation` per the spec's pure/impure split.

Uses Python's ast module so a reformatting of the function body (line
wraps, parenthesization) won't false-alarm — we're checking the call
graph, not byte-level text.
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

# Names that, if invoked anywhere inside evaluate_pull_request, prove
# the function is not pure. log.* is included because logging.handlers
# can fan out to network sinks (DD-Forwarder, OTel).
FORBIDDEN_CALL_NAMES: frozenset[str] = frozenset({
    "with_install_token_retry",
    "post_check_run",
    "httpx",
    "boto3",
})
# Attribute-access prefixes that are also banned (e.g. log.info, log.error).
FORBIDDEN_ATTR_ROOTS: frozenset[str] = frozenset({"log", "logger", "logging"})


def _violations_in_function(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    found: list[str] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name) and target.id in FORBIDDEN_CALL_NAMES:
                found.append(f"call to {target.id}() at line {node.lineno}")
            elif isinstance(target, ast.Attribute):
                root = target
                # Walk to the leftmost Name in the attribute chain
                while isinstance(root.value, ast.Attribute):
                    root = root.value
                if isinstance(root.value, ast.Name):
                    if root.value.id in FORBIDDEN_CALL_NAMES:
                        found.append(f"call to {root.value.id}.* at line {node.lineno}")
                    elif root.value.id in FORBIDDEN_ATTR_ROOTS:
                        found.append(f"call to {root.value.id}.{target.attr}(...) at line {node.lineno}")
    return found


def main() -> int:
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
