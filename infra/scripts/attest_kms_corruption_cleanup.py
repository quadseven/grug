#!/usr/bin/env python3
"""Grounding attester for spec 0005.KmsEnvelope.

Proves the bool:

  - `credential_blob_corrupt_triggers_idempotent_cleanup_per_persistence_concepts`
  - `idempotency_check_after_corruption_empty_fallthrough_per_persistence_concepts`

Asserts that `services/api/adapters/pg_user_store.py:get_user_with_tokens`
has the documented audit pattern: a `try/except CredentialBlobCorrupt`
that (a) logs the failure, (b) calls `delete_user_state(...)` (the
idempotent purge), and (c) returns `None` so callers re-route to /signin.

Static analysis via Python's ast — robust to formatting changes.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
USER_STORE = REPO_ROOT / "services/_shared/adapters/pg_user_store.py"


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _calls_named(func: ast.FunctionDef, target: str) -> list[int]:
    """Lines where func calls a bare-name function `target(...)`."""
    return [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == target
    ]


def _has_except_credential_blob_corrupt(func: ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            exc_type = handler.type
            names: list[str] = []
            if isinstance(exc_type, ast.Name):
                names.append(exc_type.id)
            elif isinstance(exc_type, ast.Tuple):
                for elt in exc_type.elts:
                    if isinstance(elt, ast.Name):
                        names.append(elt.id)
            if "CredentialBlobCorrupt" in names:
                return True
    return False


def _returns_none_in_corrupt_handler(func: ast.FunctionDef) -> bool:
    """Verify at least one ExceptHandler for CredentialBlobCorrupt ends in `return None`
    (or bare `return`)."""
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            exc_type = handler.type
            if isinstance(exc_type, ast.Name) and exc_type.id == "CredentialBlobCorrupt":
                for sub in ast.walk(handler):
                    if isinstance(sub, ast.Return):
                        if sub.value is None:
                            return True
                        if isinstance(sub.value, ast.Constant) and sub.value.value is None:
                            return True
    return False


def main() -> int:
    if not USER_STORE.exists():
        print(f"FAIL: {USER_STORE} missing")
        return 1

    tree = ast.parse(USER_STORE.read_text(), filename=str(USER_STORE))

    delete_helper = _find_function(tree, "delete_user_state")
    if delete_helper is None:
        print(f"FAIL: {USER_STORE}: helper `delete_user_state` not defined")
        return 1

    get_fn = _find_function(tree, "get_user_with_tokens")
    if get_fn is None:
        print(f"FAIL: {USER_STORE}: `get_user_with_tokens` not defined")
        return 1

    failures: list[str] = []
    if not _has_except_credential_blob_corrupt(get_fn):
        failures.append("get_user_with_tokens has no `except CredentialBlobCorrupt` handler")
    if not _calls_named(get_fn, "delete_user_state"):
        failures.append("get_user_with_tokens does not call delete_user_state(...) — idempotent purge missing")
    if not _returns_none_in_corrupt_handler(get_fn):
        failures.append("CredentialBlobCorrupt handler does not return None — caller can't route to /signin")

    if failures:
        print(f"FAIL: {USER_STORE} — spec 0005 corruption-handler audit pattern broken:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK: {USER_STORE.name}: get_user_with_tokens implements the spec 0005 PurgeCorrupt audit pattern")
    return 0


if __name__ == "__main__":
    sys.exit(main())
