#!/usr/bin/env python3
"""Grounding attester for spec 0015.ElderEvaluation.

Proves NECESSARY conditions for two bools:

  - `parse_diff_is_pure_function_no_io_per_elder_persona`
  - `evaluate_diff_is_pure_function_no_io_per_elder_persona`

The check uses an **allowlist** of permitted call targets, not a
denylist (lesson from spec 0002 peer-review HIGH). A future contributor
adding `requests.get` or `subprocess.run` to either function trips
this attester at PR time, not in production.

Sufficiency requires runtime/property testing — this is the static
half. Runtime purity is exercised by `test_evaluate_diff_is_pure_no_logging_or_io`.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

PARSE_DIFF_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "services/_shared/personas/code_reviewer/diff_parser.py",
)
EVALUATE_DIFF_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "services/_shared/personas/code_reviewer/persona.py",
)

# Allowlist of permitted call-target roots inside `parse_diff`.
# `re` is pure (compiled pattern matching, no IO).
ALLOWED_IN_PARSE_DIFF: frozenset[str] = frozenset({
    # The module-level compiled patterns we walk
    "_NEW_FILE_RE", "_DIFF_GIT_RE", "_HUNK_HEADER_RE", "_BINARY_RE",
    # Pure dataclass constructors + the parse error we raise on
    # malformed input (refusal-to-silently-swallow guard).
    "DiffHunk", "DiffParseError",
    # Stdlib pure builtins
    "tuple", "list", "set", "frozenset", "int", "str", "len", "range",
})

# Per-function explicit parameter-method allowlist. ONLY these specific
# `<param>.<method>` chains are pure-by-construction. Without the explicit
# allowlist, `_local_or_attribute_safe` previously treated every parameter
# as a safe call root — meaning `llm_response.some_io_call()` inside
# evaluate_diff would silently pass the purity attester despite spec 0015's
# no-IO claim. Caught by codex peer-review.
ALLOWED_PARAM_METHODS_PARSE_DIFF: frozenset[tuple[str, str]] = frozenset({
    # `unified_diff` is typed `str`; `.splitlines()` is pure.
    ("unified_diff", "splitlines"),
})

ALLOWED_PARAM_METHODS_EVALUATE_DIFF: frozenset[tuple[str, str]] = frozenset()

# Allowlist for `evaluate_diff` body.
ALLOWED_IN_EVALUATE_DIFF: frozenset[str] = frozenset({
    "_hunk_line_index", "_is_static_declarative_scalar",
    "CodeReviewEvaluation", "Finding",
    "tuple", "list", "set", "frozenset", "len", "all", "any", "isinstance",
})


_STRING_LITERAL_METHOD_SAFE = "<string_literal_method>"


def _call_target(call: ast.Call) -> tuple[str | None, str | None]:
    """Return `(root_name, method_or_None)` for a call target.

    `foo(...)` → `("foo", None)`. `obj.method()` → `("obj", "method")`.
    `"\\n".join(...)` → `(_STRING_LITERAL_METHOD_SAFE, None)`.
    `foo()()` → `(None, None)` (refuse to prove pure).
    """
    target = call.func
    if isinstance(target, ast.Name):
        return target.id, None
    if isinstance(target, ast.Attribute):
        root = target
        while isinstance(root.value, ast.Attribute):
            root = root.value
        if isinstance(root.value, ast.Name):
            return root.value.id, target.attr
        if isinstance(root.value, ast.Constant):
            # Method call on a constant literal (e.g. "\n".join(...)).
            # These are pure-by-construction for builtin types.
            return _STRING_LITERAL_METHOD_SAFE, None
    return None, None


def _violations_in_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    allowed: frozenset[str],
    allowed_param_methods: frozenset[tuple[str, str]],
) -> list[str]:
    found: list[str] = []
    params = {arg.arg for arg in func.args.args}
    locals_built = _locals_built_in(func)
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        root, method = _call_target(node)
        if root is None:
            found.append(
                f"non-name call target at line {node.lineno} (e.g. `foo()()`) "
                f"— refuse to prove pure"
            )
            continue
        if root == _STRING_LITERAL_METHOD_SAFE:
            continue
        # Module-level allowlist (functions, dataclass constructors,
        # builtins).
        if root in allowed:
            continue
        # Param methods — must be explicitly allowlisted as a
        # `(<param>, <method>)` pair. Without this, `llm_response.io()`
        # would silently pass.
        if root in params:
            if method is None:
                found.append(
                    f"direct call on parameter `{root}` at line {node.lineno} "
                    f"— params are not call-roots; only specific "
                    f"`(param, method)` pairs are permitted"
                )
                continue
            if (root, method) not in allowed_param_methods:
                found.append(
                    f"call `{root}.{method}` on parameter at line "
                    f"{node.lineno} — not in ALLOWED_PARAM_METHODS"
                )
            continue
        # Method calls on locals built up inside the function are
        # pure-by-construction (the local was either a list/set/dict
        # literal or assignment of a pure expression).
        if root in locals_built:
            continue
        # Known impure module → clearer error message.
        if _looks_like_stdlib_module(root):
            found.append(
                f"call to impure module `{root}` at line {node.lineno}"
            )
        else:
            found.append(
                f"call to non-allowlisted `{root}` at line {node.lineno}"
            )
    return found


# Known-impure module roots — reject by default. Mirrors the deny side
# of the allowlist for clearer error messages on the common-case escapes.
_IMPURE_MODULES: frozenset[str] = frozenset({
    "os", "sys", "io", "subprocess", "socket", "shutil",
    "httpx", "requests", "urllib", "urllib3", "aiohttp",
    "boto3", "logging", "time", "asyncio",
})


def _looks_like_stdlib_module(name: str) -> bool:
    return name in _IMPURE_MODULES


def _locals_built_in(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names introduced by an assignment INSIDE the function body
    (`body_lines = []`, `kept: list[Finding] = []`). NOT parameters —
    those are caller-controlled and could be IO-bearing objects, so
    method calls on them require explicit allowlisting (see
    ALLOWED_PARAM_METHODS_*).
    """
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _check_function(
    paths: tuple[Path, ...],
    fn_name: str,
    allowed: frozenset[str],
    allowed_param_methods: frozenset[tuple[str, str]],
) -> list[str]:
    failures: list[str] = []
    for path in paths:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        fns = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == fn_name
        ]
        if not fns:
            failures.append(f"FAIL: {path} has no {fn_name} — spec 0015 broken")
            continue
        if len(fns) != 1:
            failures.append(
                f"FAIL: {path} has {len(fns)} {fn_name} defs (expected 1)"
            )
            continue
        viols = _violations_in_function(fns[0], allowed, allowed_param_methods)
        if viols:
            failures.append(
                f"FAIL: {path} — {fn_name} is not pure:\n"
                + "\n".join(f"  {v}" for v in viols)
                + "\n  Spec 0015 attests purity. No IO inside parse_diff "
                "or evaluate_diff."
            )
    return failures


def main() -> int:
    # Vacuous-pass guards.
    if not PARSE_DIFF_PATHS or not EVALUATE_DIFF_PATHS:
        print("FAIL: spec 0015 path list empty — refusing to pass vacuously")
        return 1
    failures: list[str] = []
    failures.extend(
        _check_function(
            PARSE_DIFF_PATHS, "parse_diff",
            ALLOWED_IN_PARSE_DIFF, ALLOWED_PARAM_METHODS_PARSE_DIFF,
        )
    )
    failures.extend(
        _check_function(
            EVALUATE_DIFF_PATHS, "evaluate_diff",
            ALLOWED_IN_EVALUATE_DIFF, ALLOWED_PARAM_METHODS_EVALUATE_DIFF,
        )
    )
    if failures:
        print("\n".join(failures))
        return 1
    print(
        f"OK: parse_diff + evaluate_diff are pure in both "
        f"{len(PARSE_DIFF_PATHS)} shared persona module(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
