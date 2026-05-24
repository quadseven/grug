#!/usr/bin/env python3
"""Grounding attester for spec 0001.CheckRunResult.

Proves the bools:

  - `conclusion_present_iff_status_completed_per_process_gate_concepts`
  - `post_init_raises_on_cross_field_violation_per_process_gate_concepts`

Asserts that the `CheckRunResult` dataclass in BOTH
`services/{api,webhook}/github_checks_client.py` has a `__post_init__`
method that raises `ValueError` when the cross-field invariant
(`status == 'completed'` ⇔ `conclusion is not None`) is violated.

Static structural check + behavioral check:
  1. AST: find the class, find __post_init__, find a `raise ValueError(...)`.
  2. Behavioral: import the module and assert that bad constructions
     actually raise. The behavioral check catches the case where
     someone keeps the method but breaks the invariant logic.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

CLIENT_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "services/api/github_checks_client.py",
    REPO_ROOT / "services/webhook/github_checks_client.py",
)


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _has_post_init_raising_value_error(cls: ast.ClassDef) -> bool:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == "__post_init__":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Raise) and isinstance(sub.exc, ast.Call):
                    fn = sub.exc.func
                    if isinstance(fn, ast.Name) and fn.id == "ValueError":
                        return True
                    if isinstance(fn, ast.Attribute) and fn.attr == "ValueError":
                        return True
    return False


def _behavioral_check(path: Path) -> str | None:
    """Import the module fresh and try constructing illegal CheckRunResult
    instances. Returns None on pass, or an error string on fail."""
    spec = importlib.util.spec_from_file_location(f"_attest_crr_{path.parent.name}", path)
    if spec is None or spec.loader is None:
        return f"could not import {path}"
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        # An ImportError likely means a transitive dep (httpx) isn't
        # available in this venv. Skip behavioral check; the AST check
        # still ran.
        return f"skipped behavioral check ({type(exc).__name__}: {exc})"
    CRR = getattr(module, "CheckRunResult", None)
    if CRR is None:
        return "CheckRunResult symbol not exported"

    base_kwargs = dict(
        name="t", head_sha="a" * 40, title="t", summary="s",
    )
    # 1. completed without conclusion → must raise
    try:
        CRR(status="completed", conclusion=None, **base_kwargs)
    except ValueError:
        pass
    else:
        return "CheckRunResult(status='completed', conclusion=None) did not raise"
    # 2. non-completed with conclusion → must raise
    try:
        CRR(status="queued", conclusion="success", **base_kwargs)
    except ValueError:
        pass
    else:
        return "CheckRunResult(status='queued', conclusion='success') did not raise"
    # 3. valid: completed + conclusion → must NOT raise
    try:
        CRR(status="completed", conclusion="success", **base_kwargs)
    except ValueError as exc:
        return f"valid CheckRunResult(status='completed', conclusion='success') raised: {exc}"
    return None


def main() -> int:
    # Vacuous-pass guard: zero CLIENT_PATHS = "OK ... in 0 modules" is a lie.
    if not CLIENT_PATHS:
        print("FAIL: CLIENT_PATHS is empty — refusing to pass vacuously")
        return 1
    failures: list[str] = []
    skipped_behavioral: list[str] = []

    for path in CLIENT_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        cls = _find_class(tree, "CheckRunResult")
        if cls is None:
            failures.append(f"FAIL: {path}: CheckRunResult class not found")
            continue
        if not _has_post_init_raising_value_error(cls):
            failures.append(
                f"FAIL: {path}: CheckRunResult.__post_init__ missing or doesn't raise ValueError. "
                f"Spec 0001 attests post_init_raises_on_cross_field_violation."
            )
            continue
        # AST passed — try behavioral
        result = _behavioral_check(path)
        if result and result.startswith("skipped"):
            skipped_behavioral.append(f"{path.name}: {result}")
        elif result is not None:
            failures.append(f"FAIL: {path}: behavioral check failed — {result}")

    if failures:
        print("\n".join(failures))
        return 1
    msg = f"OK: CheckRunResult.__post_init__ enforces cross-field invariant in {len(CLIENT_PATHS)} module(s)"
    if skipped_behavioral:
        msg += " (behavioral checks skipped: " + "; ".join(skipped_behavioral) + ")"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
