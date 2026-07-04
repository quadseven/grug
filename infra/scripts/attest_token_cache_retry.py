#!/usr/bin/env python3
"""Grounding attester for spec 0004.TokenCache.

Proves a NECESSARY condition for these bools:

  - `with_install_token_retry_invalidates_then_refetches_once_per_identity_concepts`
  - `retry_does_not_loop_on_repeated_401_per_identity_concepts`

Asserts that `services/{api,webhook}/github_app_auth/__init__.py:with_install_token_retry`:
  1. Catches `httpx.HTTPStatusError`.
  2. Checks `status_code == 401` (or `!= 401: raise`) — non-401 propagates.
  3. Calls `get_install_token(...)` with `force_refresh=True` (or kwarg-equivalent).
  4. Calls `fn(token)` EXACTLY TWICE in total (one initial + one retry) — no
     loop, no third attempt. The "exactly twice" guarantee is what makes the
     retry safe against perma-401 (revoked App perms): the second 401
     propagates instead of looping forever.

Sufficiency requires runtime testing — this static check proves the structural
shape of the retry-once invariant.
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


def _exactly_two_fn_calls(func: ast.FunctionDef) -> bool:
    """Body must call `fn(token)` exactly twice — once initial, once on retry.
    A third call would mean an unbounded loop; zero/one would mean no retry."""
    fn_call_count = 0
    for node in ast.walk(func):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "fn"):
            fn_call_count += 1
    return fn_call_count == 2


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
            problems.append("no `status_code == 401` (or `!= 401`) check — retry would fire on 5xx too")
        if not _has_force_refresh_call(retry):
            problems.append("no `get_install_token(..., force_refresh=True)` call — cache invalidation missing")
        if not _exactly_two_fn_calls(retry):
            problems.append("`fn(token)` is not called exactly twice — risks unbounded retry loop OR no retry at all")
        if problems:
            failures.append(f"FAIL: {path}:\n" + "\n".join(f"  - {p}" for p in problems))

    if failures:
        print("\n".join(failures))
        return 1
    print(f"OK: with_install_token_retry semantics intact in {len(AUTH_PATHS)} module(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
