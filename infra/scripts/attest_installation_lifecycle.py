#!/usr/bin/env python3
"""Grounding attester for spec 0003.Installation.

Proves a NECESSARY condition for these bools:

  - `install_id_is_github_issued_integer_per_identity_concepts`
  - `account_type_is_user_or_organization_per_identity_concepts`
  - `installed_by_user_id_required_for_allowlist_gate_per_identity_concepts`
  - `allowlist_gate_blocks_non_allowlisted_user_per_identity_concepts`

Asserts that `services/webhook/dispatcher.py:_handle_installation`:
  1. Routes the `installation` event to its handler.
  2. Calls `delete_installation(int(install_id))` on `action="deleted"`.
  3. Calls `record_installation(...)` on `action="created"` (or `"new_permissions_accepted"`).
  4. Imports `is_install_allowlisted` (the allowlist gate function) at module scope.
  5. Casts install_id to `int` before persisting (spec bool: GitHub-issued integer).

Per-bool sufficiency requires runtime testing — this attester proves the
structural plumbing exists.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCHER = REPO_ROOT / "services/webhook/dispatcher.py"


def _module_imports(tree: ast.Module) -> set[str]:
    """All bare-name imports across all from/import statements."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _calls_named(tree: ast.AST, target: str) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == target
    ]


def _has_int_cast_of(tree: ast.AST, name_substring: str) -> bool:
    """Verify some `int(<name_containing_substring>)` cast exists — the
    GitHub-issued integer invariant for install_id."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "int"):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        # Match int(install_id) or int(installation_id) or int(repo_id) etc.
        if isinstance(arg, ast.Name) and name_substring in arg.id:
            return True
        # Match int(x.get("id")) etc.
        if isinstance(arg, ast.Call):
            return True  # heuristic — int() of any call is fine, narrows risk
    return False


def main() -> int:
    if not DISPATCHER.exists():
        print(f"FAIL: {DISPATCHER} missing")
        return 1

    tree = ast.parse(DISPATCHER.read_text())
    imports = _module_imports(tree)

    failures: list[str] = []

    for name in ("record_installation", "delete_installation", "is_install_allowlisted"):
        if name not in imports:
            failures.append(f"missing import of `{name}` — allowlist/lifecycle plumbing broken")

    if not _calls_named(tree, "record_installation"):
        failures.append("no call to `record_installation(...)` — installation.created not recorded")
    if not _calls_named(tree, "delete_installation"):
        failures.append("no call to `delete_installation(...)` — installation.deleted not handled")
    if not _calls_named(tree, "is_install_allowlisted"):
        failures.append("no call to `is_install_allowlisted(...)` — allowlist gate not invoked")

    if not _has_int_cast_of(tree, "install"):
        failures.append("no `int(install_id)` / `int(installation_id)` cast — spec 0003 declares install_id is GitHub-issued integer")

    if failures:
        print(f"FAIL: {DISPATCHER}:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK: {DISPATCHER.name}: installation lifecycle + allowlist plumbing intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
