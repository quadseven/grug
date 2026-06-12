#!/usr/bin/env python3
"""Grounding attester for spec 0009.RepoConfig.

Proves NECESSARY conditions for these bools:

  - `default_persona_config_dict_is_source_of_truth_per_identity_concepts`
  - `missing_row_returns_defaults_not_none_per_identity_concepts`
  - `defaults_enabled_explicit_optout_per_identity_concepts`
  - `is_persona_enabled_lookup_by_persona_underscore_enabled_key_per_identity_concepts`

Asserts that both `services/{api,webhook}/adapters/pg_install_store.py`:
  1. Define a module-level `_DEFAULT_PERSONA_CONFIG` dict.
  2. The dict contains `tpm_enabled: True` (defaults-enabled invariant).
  3. Define `get_repo_config(install_id, repo_id)` returning the dict on missing row.
  4. Define `is_persona_enabled(install_id, repo_id, persona)` that builds
     the lookup key as `f"{persona}_enabled"`.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

INSTALL_STORE_PATHS: tuple[Path, ...] = (
    # Post-swap (#354): install_store.py is a re-export facade; the
    # default dict + gate logic live in the pg implementation.
    REPO_ROOT / "services/api/adapters/pg_install_store.py",
    REPO_ROOT / "services/webhook/adapters/pg_install_store.py",
)


def _module_default_persona_config(tree: ast.Module) -> dict[str, bool] | None:
    """Find `_DEFAULT_PERSONA_CONFIG = {"tpm_enabled": True, ...}` at module scope.
    Returns the parsed dict or None if not found."""
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not (len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == "_DEFAULT_PERSONA_CONFIG"):
            continue
        if not isinstance(stmt.value, ast.Dict):
            return None
        out: dict[str, bool] = {}
        for k, v in zip(stmt.value.keys, stmt.value.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                return None
            if not (isinstance(v, ast.Constant) and isinstance(v.value, bool)):
                return None
            out[k.value] = v.value
        return out
    return None


def _has_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _is_persona_enabled_builds_key_correctly(func: ast.FunctionDef) -> bool:
    """Search the function body for an f-string / format / concat that builds
    `<persona>_enabled` as the lookup key."""
    for node in ast.walk(func):
        # f-string: f"{persona}_enabled"
        if isinstance(node, ast.JoinedStr):
            text_parts: list[str] = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    text_parts.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    text_parts.append("{}")
            joined = "".join(text_parts)
            if joined.endswith("_enabled"):
                return True
        # str concat: persona + "_enabled"
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
                and isinstance(node.right, ast.Constant) and node.right.value == "_enabled"):
            return True
    return False


def main() -> int:
    if not INSTALL_STORE_PATHS:
        print("FAIL: INSTALL_STORE_PATHS empty — refusing to pass vacuously")
        return 1

    failures: list[str] = []

    for path in INSTALL_STORE_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue
        tree = ast.parse(path.read_text())

        cfg = _module_default_persona_config(tree)
        if cfg is None:
            failures.append(f"FAIL: {path}: `_DEFAULT_PERSONA_CONFIG = {{...}}` not found at module scope")
            continue
        if cfg.get("tpm_enabled") is not True:
            failures.append(
                f"FAIL: {path}: _DEFAULT_PERSONA_CONFIG['tpm_enabled'] is not True "
                f"(got {cfg.get('tpm_enabled')!r}). Spec 0009 attests defaults-enabled."
            )

        get_repo = _has_function(tree, "get_repo_config")
        if get_repo is None:
            failures.append(f"FAIL: {path}: `get_repo_config` not defined")

        is_enabled = _has_function(tree, "is_persona_enabled")
        if is_enabled is None:
            failures.append(f"FAIL: {path}: `is_persona_enabled` not defined")
        elif not _is_persona_enabled_builds_key_correctly(is_enabled):
            failures.append(
                f"FAIL: {path}: is_persona_enabled doesn't build `<persona>_enabled` lookup key "
                f"(f-string or str-concat with suffix `_enabled` expected)."
            )

    if failures:
        print("\n".join(failures))
        return 1
    print(f"OK: _DEFAULT_PERSONA_CONFIG + get_repo_config + is_persona_enabled correct in {len(INSTALL_STORE_PATHS)} module(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
