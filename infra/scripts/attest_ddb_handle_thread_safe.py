#!/usr/bin/env python3
"""Grounding attester for the DDB-handle concurrency invariant.

The peer-review pass surfaced an unguarded lazy-init race in the
`_LazyTable` descriptor pattern shared by user_store + install_store:
two warm-Lambda concurrent invocations could both see `_table_real is
None` and both call `boto3.resource()`, leaking one of the two handles.

Asserts that in every file using `_LazyTable`:
  1. `import threading` is present
  2. `_init_lock = threading.Lock()` (or similar) is at module scope
  3. `_LazyTable.__getattr__` opens the lock BEFORE the inner re-check
  4. The double-checked-locking re-check (`if _table_real is None:` AFTER
     `with _init_lock:`) is present

No spec dir for this invariant — the attester IS the contract. If we
later promote it to a full spec, the bool name would be
`ddb_handle_lazy_init_is_double_checked_locked_per_persistence_concepts`.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

LAZY_TABLE_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "services/api/adapters/user_store.py",
    REPO_ROOT / "services/api/adapters/install_store.py",
    REPO_ROOT / "services/webhook/adapters/install_store.py",
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


def _check_lazy_table_getattr(tree: ast.Module) -> str | None:
    """Find class _LazyTable, find __getattr__, verify it uses `with <lock>:`
    AND has the inner `if ... is None:` re-check inside the `with` block."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "_LazyTable"):
            continue
        for body_node in node.body:
            if not (isinstance(body_node, ast.FunctionDef) and body_node.name == "__getattr__"):
                continue
            with_blocks = [n for n in ast.walk(body_node) if isinstance(n, ast.With)]
            if not with_blocks:
                return "__getattr__ has no `with <lock>:` block"
            # Verify at least one `with` contains an inner `if ... is None:`
            for w in with_blocks:
                for sub in ast.walk(w):
                    if isinstance(sub, ast.If) and isinstance(sub.test, ast.Compare):
                        ops = sub.test.ops
                        comps = sub.test.comparators
                        if (
                            len(ops) == 1
                            and isinstance(ops[0], ast.Is)
                            and len(comps) == 1
                            and isinstance(comps[0], ast.Constant)
                            and comps[0].value is None
                        ):
                            return None
            return "`with <lock>:` block has no inner `if ... is None:` re-check (double-checked locking incomplete)"
        return "_LazyTable has no __getattr__"
    return "no _LazyTable class found"


def main() -> int:
    failures: list[str] = []
    for path in LAZY_TABLE_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        problems: list[str] = []
        if not _has_threading_import(tree):
            problems.append("no `import threading` at module level")
        if not _has_module_lock(tree):
            problems.append("no `threading.Lock()` assigned at module scope")
        getattr_issue = _check_lazy_table_getattr(tree)
        if getattr_issue:
            problems.append(getattr_issue)
        if problems:
            failures.append(f"FAIL: {path}\n" + "\n".join(f"  - {p}" for p in problems))
    if failures:
        print("\n".join(failures))
        print("\n  Fix: add `_init_lock = threading.Lock()` and wrap the lazy-init in")
        print("  `with _init_lock:` followed by an inner `if _table_real is None:` re-check.")
        return 1
    print(f"OK: _LazyTable double-checked-locking verified in {len(LAZY_TABLE_PATHS)} module(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
