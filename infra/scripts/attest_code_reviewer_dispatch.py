#!/usr/bin/env python3
"""Grounding attester for spec 0017.CodeReviewerDispatch.

Proves NECESSARY (static, AST-based) conditions for the dispatch
orchestration bools against the real mirrored `dispatch.py`:

  Gate contract (`_publish_shape` + mode derivation):
    - mode_derives_from_repo_config_blocking_flag_per_dispatch
    - advisory_mode_forces_neutral_and_comment_per_dispatch
    - blocking_failure_yields_failure_and_request_changes_per_dispatch
    - publish_shape_single_source_of_truth_for_conclusion_and_event_per_dispatch

  Publish-independence contract (`dispatch_code_review`):
    - check_run_and_review_publish_independently_per_dispatch
    - dispatch_never_raises_wire_exception_degrades_to_neutral_per_dispatch

Static half only — these are necessary, not sufficient. Runtime
sufficiency (the gate actually returning neutral in advisory mode, the
review post still firing after a check-run 5xx) is exercised by
`tests/test_code_reviewer_dispatch.py`. The point of the static check is
to trip at PR time the moment the code drifts from the spec — e.g. someone
adds an early `return` to the check-run except handler (which would make a
check-run 5xx silently skip the review post) or collapses `_publish_shape`'s
two-literal gate.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DISPATCH_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "services/api/personas/code_reviewer/dispatch.py",
    REPO_ROOT / "services/webhook/personas/code_reviewer/dispatch.py",
)


def _find_funcdef(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    defs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == name
    ]
    return defs[0] if len(defs) == 1 else None


def _calls_named(node: ast.AST | list[ast.stmt], name: str) -> list[ast.Call]:
    """All Call nodes under `node` (a node or a list of stmts) whose callee
    is the bare name `name`."""
    roots = node if isinstance(node, list) else [node]
    out: list[ast.Call] = []
    for root in roots:
        for n in ast.walk(root):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name:
                out.append(n)
    return out


def _const_tuple(node: ast.AST) -> tuple[object, ...] | None:
    """A literal tuple of constants, e.g. ("neutral", "COMMENT") → that
    pair. Anything else → None."""
    if isinstance(node, ast.Tuple) and all(isinstance(e, ast.Constant) for e in node.elts):
        return tuple(e.value for e in node.elts)  # type: ignore[attr-defined]
    return None


def _return_tuples(func: ast.FunctionDef) -> set[tuple[object, ...]]:
    out: set[tuple[object, ...]] = set()
    for n in ast.walk(func):
        if isinstance(n, ast.Return) and n.value is not None:
            t = _const_tuple(n.value)
            if t is not None:
                out.add(t)
    return out


def _has_blocking_ternary(func: ast.FunctionDef) -> bool:
    """A `... "blocking" if blocking else "advisory" ...` IfExp whose test
    is the `blocking` param — proving mode derives from the flag."""
    for n in ast.walk(func):
        if isinstance(n, ast.IfExp) and isinstance(n.test, ast.Name) and n.test.id == "blocking":
            vals = {
                getattr(n.body, "value", None),
                getattr(n.orelse, "value", None),
            }
            if {"blocking", "advisory"} <= vals:
                return True
    return False


def _try_blocks_containing(func: ast.FunctionDef, callee: str) -> list[ast.Try]:
    return [
        n for n in ast.walk(func)
        if isinstance(n, ast.Try) and _calls_named(n.body, callee)
    ]


def _check_publish_shape(tree: ast.AST, path: Path) -> list[str]:
    f = _find_funcdef(tree, "_publish_shape")
    if f is None:
        return [f"FAIL: {path} — exactly one `_publish_shape` expected (gate SSOT)"]
    fails: list[str] = []
    rets = _return_tuples(f)
    if ("neutral", "COMMENT") not in rets:
        fails.append(
            f"FAIL: {path} — _publish_shape never returns ('neutral','COMMENT'); "
            "advisory/degraded must force neutral+COMMENT"
        )
    if ("failure", "REQUEST_CHANGES") not in rets:
        fails.append(
            f"FAIL: {path} — _publish_shape never returns ('failure','REQUEST_CHANGES'); "
            "blocking failure must request changes"
        )
    # advisory/degraded guard must reference BOTH the mode and degraded_reason
    src = ast.unparse(f)
    if '"advisory"' not in src and "'advisory'" not in src:
        fails.append(f"FAIL: {path} — _publish_shape does not gate on advisory mode")
    if "degraded_reason" not in src:
        fails.append(
            f"FAIL: {path} — _publish_shape ignores degraded_reason; a degraded "
            "evaluation must force advisory regardless of mode"
        )
    return fails


def _check_dispatch(tree: ast.AST, path: Path) -> list[str]:
    f = _find_funcdef(tree, "dispatch_code_review")
    if f is None:
        return [f"FAIL: {path} — exactly one `dispatch_code_review` expected"]
    fails: list[str] = []

    # mode derives from the blocking flag
    blocking_params = {a.arg for a in (f.args.args + f.args.kwonlyargs)}
    if "blocking" not in blocking_params:
        fails.append(f"FAIL: {path} — dispatch_code_review has no `blocking` param")
    elif not _has_blocking_ternary(f):
        fails.append(
            f"FAIL: {path} — mode is not derived as "
            "`'blocking' if blocking else 'advisory'`"
        )

    # check-run publish + review publish exist on independent surfaces
    check_tries = _try_blocks_containing(f, "post_check_run")
    if not check_tries:
        fails.append(f"FAIL: {path} — post_check_run is not wrapped in try/except")
    if not _calls_named(f, "post_review"):
        fails.append(f"FAIL: {path} — no post_review call (independent surface missing)")

    # INDEPENDENCE: the check-run except handler must NOT early-return, or a
    # check-run 5xx would skip the review post. It must record the failure.
    for tnode in check_tries:
        for handler in tnode.handlers:
            if any(
                isinstance(n, ast.Return)
                for stmt in handler.body for n in ast.walk(stmt)
            ):
                fails.append(
                    f"FAIL: {path} — check-run except handler returns early; a "
                    "check-run publish failure would skip the independent review post"
                )
        handler_src = "".join(ast.unparse(h) for h in tnode.handlers)
        if "check_publish_failed" not in handler_src:
            fails.append(
                f"FAIL: {path} — check-run except does not record check_publish_failed"
            )

    # never-raise: fetch/parse degrade path present (_publish_degraded) and
    # _resolve_result consults BOTH publish-failed flags.
    if not _calls_named(f, "_publish_degraded"):
        fails.append(
            f"FAIL: {path} — no _publish_degraded fallback; fetch/parse errors "
            "must degrade to advisory-neutral, not raise"
        )
    resolve_calls = _calls_named(f, "_resolve_result")
    if not resolve_calls:
        fails.append(f"FAIL: {path} — _resolve_result not called")
    else:
        kwargs = {kw.arg for c in resolve_calls for kw in c.keywords}
        for needed in ("check_publish_failed", "review_publish_failed"):
            if needed not in kwargs:
                fails.append(
                    f"FAIL: {path} — _resolve_result not passed `{needed}`; result "
                    "rollup must consult both independent publish surfaces"
                )
    return fails


def main() -> int:
    if not DISPATCH_PATHS:
        print("FAIL: spec 0017 path list empty — refusing to pass vacuously")
        return 1
    failures: list[str] = []
    for path in DISPATCH_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        failures.extend(_check_publish_shape(tree, path))
        failures.extend(_check_dispatch(tree, path))
    if failures:
        print("\n".join(failures))
        print("\nSpec 0017 attests the dispatch gate + dual-publish independence.")
        return 1
    print(
        f"OK: _publish_shape gate + dual-publish independence verified in "
        f"{len(DISPATCH_PATHS)} mirrored dispatch module(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
