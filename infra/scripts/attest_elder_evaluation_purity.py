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
    REPO_ROOT / "services/api/personas/code_reviewer/diff_parser.py",
    REPO_ROOT / "services/webhook/personas/code_reviewer/diff_parser.py",
)
EVALUATE_DIFF_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "services/api/personas/code_reviewer/persona.py",
    REPO_ROOT / "services/webhook/personas/code_reviewer/persona.py",
)

# Allowlist of permitted call-target roots inside `parse_diff`.
# `re` is pure (compiled pattern matching, no IO).
ALLOWED_IN_PARSE_DIFF: frozenset[str] = frozenset({
    # The module-level compiled patterns we walk
    "_NEW_FILE_RE", "_DIFF_GIT_RE", "_HUNK_HEADER_RE", "_BINARY_RE",
    # Pure dataclass constructors
    "DiffHunk",
    # Stdlib pure builtins
    "tuple", "list", "set", "frozenset", "int", "str", "len", "range",
})

# Allowlist for `evaluate_diff` body.
ALLOWED_IN_EVALUATE_DIFF: frozenset[str] = frozenset({
    "_hunk_line_index",
    "CodeReviewEvaluation", "Finding",
    "tuple", "list", "set", "frozenset", "len", "all", "any", "isinstance",
})


_STRING_LITERAL_METHOD_SAFE = "<string_literal_method>"


def _call_target_name(call: ast.Call) -> str | None:
    """Return the leftmost-name of the call target. For `re.compile(...)`
    returns `re`; for `_helper(...)` returns `_helper`. For
    `"\\n".join(...)` returns a sentinel (string-literal methods are
    pure). None for `foo()()` (refuse to prove pure on call-of-call)."""
    target = call.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        root = target
        while isinstance(root.value, ast.Attribute):
            root = root.value
        if isinstance(root.value, ast.Name):
            return root.value.id
        if isinstance(root.value, ast.Constant):
            # Method call on a constant literal (e.g. "\n".join(...)).
            # These are pure-by-construction for builtin types.
            return _STRING_LITERAL_METHOD_SAFE
    return None


def _violations_in_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef, allowed: frozenset[str],
) -> list[str]:
    found: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = _call_target_name(node)
        if name is None:
            found.append(
                f"non-name call target at line {node.lineno} (e.g. `foo()()`) "
                f"— refuse to prove pure"
            )
            continue
        if name == _STRING_LITERAL_METHOD_SAFE:
            continue
        if name not in allowed:
            # Method calls on parameters or locals (e.g. `body_lines.append(...)`)
            # are pure-by-construction for builtin types. The attester can't
            # distinguish param-method from module-fn without type info; we
            # accept method-on-Attribute as a known-pure pattern.
            # But only when the root name is a local variable, not a module.
            # Heuristic: if `name` is lowercase + matches a known stdlib
            # module (`os`, `sys`, `httpx`, etc.), this is an import call
            # and must be allowlisted.
            if _looks_like_stdlib_module(name):
                found.append(
                    f"call to non-allowlisted module `{name}` at line {node.lineno}"
                )
            elif name not in _local_or_attribute_safe(func):
                found.append(
                    f"call to non-allowlisted `{name}` at line {node.lineno}"
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


def _local_or_attribute_safe(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names that are function-local (params, assignments) — calls on
    these are assumed pure-by-construction (param method calls on
    builtin types like `list.append`)."""
    names: set[str] = set()
    for arg in func.args.args:
        names.add(arg.arg)
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # `body_lines: list[str] = [line]` — typed assignment that
            # introduces a local. Without this branch the attester
            # mistakes method calls on the local for module-level calls.
            names.add(node.target.id)
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            # `for h in hunks:` — loop variable is a local.
            names.add(node.target.id)
    return names


def _check_function(
    paths: tuple[Path, ...], fn_name: str, allowed: frozenset[str],
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
        viols = _violations_in_function(fns[0], allowed)
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
        _check_function(PARSE_DIFF_PATHS, "parse_diff", ALLOWED_IN_PARSE_DIFF)
    )
    failures.extend(
        _check_function(EVALUATE_DIFF_PATHS, "evaluate_diff", ALLOWED_IN_EVALUATE_DIFF)
    )
    if failures:
        print("\n".join(failures))
        return 1
    print(
        f"OK: parse_diff + evaluate_diff are pure in both "
        f"{len(PARSE_DIFF_PATHS)} mirrored persona module(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
