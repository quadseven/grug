#!/usr/bin/env python3
"""Grounding attester for the store-handle concurrency invariant.

The peer-review pass surfaced an unguarded lazy-init race in the DDB
era's `_LazyTable` descriptor: two warm concurrent invocations could
both see the handle as None and both construct it, leaking one. The
#354 Postgres port carries the SAME invariant in `pg_base.get_pool()`
(its docstring cites the same rationale): a connection pool built
twice leaks connections and double-runs the schema bootstrap.

Asserts that in every pg_base mirror:
  1. `import threading` is present
  2. `<name> = threading.Lock()` is at module scope
  3. `get_pool` opens a `with <lock>:` block containing the inner
     `if _pool is None:` re-check (double-checked locking, testing
     the RIGHT variable - a spurious `if other is None:` must not pass)

No spec dir for this invariant - the attester IS the contract (the
spec-0011 automaton models the state machine; this grounds it in code).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

POOL_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "services/api/adapters/pg_base.py",
    REPO_ROOT / "services/webhook/adapters/pg_base.py",
)


def _has_threading_import(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "threading":
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module == "threading":
            return True
    return False


def _has_module_lock(tree: ast.Module) -> bool:
    """Look for `<name> = threading.Lock()` at module scope."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "threading"
            and node.value.func.attr == "Lock"
        ):
            continue
        return True
    return False


def _check_get_pool_double_checked(tree: ast.Module) -> str | None:
    """Find get_pool, verify it uses `with <lock>:` AND has the inner
    `if _pool is None:` re-check inside the `with` block."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "get_pool"):
            continue
        with_blocks = [n for n in ast.walk(node) if isinstance(n, ast.With)]
        if not with_blocks:
            return "get_pool has no `with <lock>:` block"
        for w in with_blocks:
            for sub in ast.walk(w):
                if isinstance(sub, ast.If) and isinstance(sub.test, ast.Compare):
                    ops = sub.test.ops
                    comps = sub.test.comparators
                    # Only accept the re-check when the name being tested
                    # is `_pool` - a spurious `if other_var is None:`
                    # would otherwise false-pass (peer-review MED, 4x).
                    if not (
                        len(ops) == 1
                        and isinstance(ops[0], ast.Is)
                        and len(comps) == 1
                        and isinstance(comps[0], ast.Constant)
                        and comps[0].value is None
                    ):
                        continue
                    if not (isinstance(sub.test.left, ast.Name) and sub.test.left.id == "_pool"):
                        continue
                    return None
        return (
            "`with <lock>:` block has no inner `if _pool is None:` re-check "
            "(double-checked locking incomplete or testing wrong variable)"
        )
    return "no get_pool function found"


def main() -> int:
    # Vacuous-pass guard.
    if not POOL_PATHS:
        print("FAIL: POOL_PATHS is empty — refusing to pass vacuously")
        return 1
    failures: list[str] = []
    for path in POOL_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        problems: list[str] = []
        if not _has_threading_import(tree):
            problems.append("no `import threading` at module level")
        if not _has_module_lock(tree):
            problems.append("no `threading.Lock()` assigned at module scope")
        get_pool_issue = _check_get_pool_double_checked(tree)
        if get_pool_issue:
            problems.append(get_pool_issue)
        if problems:
            failures.append(f"FAIL: {path}\n" + "\n".join(f"  - {p}" for p in problems))
    if failures:
        print("\n".join(failures))
        print("\n  Fix: add `_pool_lock = threading.Lock()` and wrap the lazy-init in")
        print("  `with _pool_lock:` followed by an inner `if _pool is None:` re-check.")
        return 1
    print(f"OK: get_pool double-checked-locking verified in {len(POOL_PATHS)} module(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
