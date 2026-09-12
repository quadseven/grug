#!/usr/bin/env python3
"""Grounding attester for the TokenCache contract.

Proves a NECESSARY condition for these bools:

  - `with_install_token_retry_invalidates_then_refetches_once_per_identity_concepts`
  - `retry_does_not_loop_on_repeated_401_per_identity_concepts`
  - `transient_github_errors_retry_within_a_bound_per_identity_concepts` (#946)

Asserts that `services/{api,webhook}/github_app_auth/__init__.py:with_install_token_retry`:
  1. Catches `httpx.HTTPStatusError`.
  2. Checks `status_code == 401` (or `!= 401: raise`) — the 401 path is
     still distinguished from the general retry path.
  3. Calls `get_install_token(...)` with `force_refresh=True` (or kwarg-equivalent).
  4. Calls `fn(token)` from inside a `for ... in range(...)` loop, never a
     bare `while True` or unconditional recursion. #946 widened this from a
     fixed "exactly twice" (401-only) shape to a bounded retry that also
     covers 5xx/secondary-rate-limit - the loop-over-`range` shape is what
     keeps ANY retry class, present or future, provably finite: `range`
     cannot iterate forever, so this is a structural bound rather than a
     count of literal call sites in the source (which #946 collapsed from
     two `return fn(token)` sites to one, inside the loop).

Sufficiency requires runtime testing — this static check proves the structural
shape of the bounded-retry invariant, not the retry policy's specific values.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

AUTH_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "services/_shared/github_app_auth/__init__.py",
)


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _catches_http_status_error(func: ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            exc = handler.type
            if isinstance(exc, ast.Attribute) and exc.attr == "HTTPStatusError":
                return True
            if isinstance(exc, ast.Name) and exc.id == "HTTPStatusError":
                return True
            if isinstance(exc, ast.Tuple):
                for elt in exc.elts:
                    if isinstance(elt, ast.Attribute) and elt.attr == "HTTPStatusError":
                        return True
    return False


def _checks_401_status(func: ast.FunctionDef) -> bool:
    """Verify the handler body has a comparison against 401 (the only retryable code).
    Without this check the retry would fire on every HTTPStatusError including 5xx,
    which would double the load on GH outages."""
    for node in ast.walk(func):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute):
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and comp.value == 401:
                    return True
        # Also `status_code != 401`
        if isinstance(node, ast.Constant) and node.value == 401:
            return True
    return False


def _has_force_refresh_call(func: ast.FunctionDef) -> bool:
    """Verify get_install_token(..., force_refresh=True) appears in retry path."""
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "get_install_token"):
            continue
        for kw in node.keywords:
            if kw.arg == "force_refresh" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return True
    return False


def _fn_call_is_bounded(func: ast.FunctionDef) -> bool:
    """#946: `fn(token)` must be called from inside a `for ... in range(...)`
    loop - a structural proof the retry is finite regardless of how many
    retry CLASSES exist (401, 5xx, secondary rate limit), without pinning
    the exact attempt count (a policy value, not a structural invariant).
    A `while True` or a call outside any loop both fail this - the first
    because it cannot be proven to terminate by inspection, the second
    because it means no retry at all."""
    def _is_fn_call(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "fn"

    def _is_range_call(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "range"

    for node in ast.walk(func):
        if isinstance(node, ast.For) and _is_range_call(node.iter):
            if any(_is_fn_call(inner) for inner in ast.walk(node)):
                return True
    return False


def main() -> int:
    if not AUTH_PATHS:
        print("FAIL: AUTH_PATHS empty — refusing to pass vacuously")
        return 1

    failures: list[str] = []

    for path in AUTH_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue
        tree = ast.parse(path.read_text())
        retry = _find_function(tree, "with_install_token_retry")
        if retry is None:
            failures.append(f"FAIL: {path}: `with_install_token_retry` not defined")
            continue
        problems: list[str] = []
        if not _catches_http_status_error(retry):
            problems.append("no `except httpx.HTTPStatusError` handler")
        if not _checks_401_status(retry):
            problems.append("no `status_code == 401` (or `!= 401`) check — the 401 refresh path is gone")
        if not _has_force_refresh_call(retry):
            problems.append("no `get_install_token(..., force_refresh=True)` call — cache invalidation missing")
        if not _fn_call_is_bounded(retry):
            problems.append(
                "`fn(token)` is not called from inside a `for ... in range(...)` loop "
                "— risks an unbounded retry loop OR no retry at all"
            )
        if problems:
            failures.append(f"FAIL: {path}:\n" + "\n".join(f"  - {p}" for p in problems))

    if failures:
        print("\n".join(failures))
        return 1
    print(f"OK: with_install_token_retry semantics intact in {len(AUTH_PATHS)} module(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
