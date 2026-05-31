#!/usr/bin/env python3
"""Grounding attester for spec 0017.CodeReviewerDispatch.

Proves NECESSARY (static, AST-based) conditions for the dispatch
orchestration bools against the real mirrored `dispatch.py`.

Design note — value-flow, not token-presence. An earlier draft checked
that string literals / call names merely *appeared* in the function; a
silent-failure audit (PR #256) showed every such check was defeatable by a
one-line mutation that kept the token while breaking behavior (dead `if
False` branch, `mode="advisory"` literal threaded into the gate, a flag
logged-as-string but never set, an `except ValueError` that can't catch a
wire 5xx). This version asserts AST *shape and reachability*:

  Gate contract (`_publish_shape` + mode derivation):
    - mode_derives_from_repo_config_blocking_flag_per_dispatch
        → `mode` is assigned the `"blocking" if blocking else "advisory"`
          ternary and NEVER reassigned to a constant; the value passed to
          `_publish_shape` is the `mode` Name, not a literal.
    - advisory_mode_forces_neutral_and_comment_per_dispatch
      + degraded_evaluation_forces_advisory_regardless_of_mode_per_dispatch
        → the branch returning ("neutral","COMMENT") is guarded by an
          OR over `mode == "advisory"` and `…degraded_reason`.
    - blocking_failure_yields_failure_and_request_changes_per_dispatch
        → ("failure","REQUEST_CHANGES") is returned under a non-constant
          test referencing `conclusion`/`"failure"`.
    - publish_shape_single_source_of_truth_for_conclusion_and_event_per_dispatch
        → exactly one `_publish_shape`, returning 2-tuples; no dead
          (`if False`) branches anywhere in the gate or dispatch.

  Publish-independence contract (`dispatch_code_review`):
    - check_run_and_review_publish_independently_per_dispatch
        → post_check_run + post_review each in their own try; the
          check-run handler sets `check_publish_failed = True` (an Assign,
          not a string) and does NOT early-return; `_resolve_result` is
          passed both publish-failed kwargs.
    - dispatch_never_raises_wire_exception_degrades_to_neutral_per_dispatch
        → every wire-call try catches an allowed wire exception type
          (httpx.*/DiffParseError/Exception), and `_publish_degraded`
          exists as the fetch/parse fallback.

Static half only — runtime sufficiency (the gate actually returning neutral
in advisory mode, the review post still firing after a check-run 5xx) is
exercised by `services/webhook/tests/test_code_reviewer_dispatch.py`. That
runtime suite lives on the webhook side only; this attester covers BOTH
mirrored source modules statically.
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

# Exception types that legitimately wrap a wire/IO call so dispatch can
# degrade instead of raising. `except ValueError` around post_check_run
# would NOT catch a GitHub 5xx → rejected.
ALLOWED_WIRE_EXC: frozenset[str] = frozenset({
    "HTTPStatusError", "RequestError", "DiffParseError", "Exception",
})

NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _find_funcdef(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    defs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == name
    ]
    return defs[0] if len(defs) == 1 else None


def _calls_named(node: ast.AST | list[ast.stmt], name: str) -> list[ast.Call]:
    roots = node if isinstance(node, list) else [node]
    out: list[ast.Call] = []
    for root in roots:
        for n in ast.walk(root):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name:
                out.append(n)
    return out


def _const_tuple(node: ast.AST) -> tuple[object, ...] | None:
    if isinstance(node, ast.Tuple) and all(isinstance(e, ast.Constant) for e in node.elts):
        return tuple(e.value for e in node.elts)  # type: ignore[attr-defined]
    return None


def _is_falsy_const(node: ast.AST) -> bool:
    """A statically-falsy literal usable as a dead-code guard: a falsy
    constant (`False`, `0`, `None`, `""`) OR an empty collection literal
    (`[]`, `()`, `{}`, set/dict). Covers the AST forms an `if <X>:` guard
    can take to be unreachable at runtime while still parsing."""
    if isinstance(node, ast.Constant):
        return not node.value
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return len(node.elts) == 0
    if isinstance(node, ast.Dict):
        return len(node.keys) == 0
    return False


def _walk_no_nested(node: ast.AST):
    """ast.walk but pruning nested def/lambda subtrees — so a helper or
    lambda inside a handler doesn't leak its `return` into our scan. A node
    that IS itself a nested scope is pruned entirely (yields nothing)."""
    if isinstance(node, NESTED_SCOPES):
        return
    todo = [node]
    while todo:
        n = todo.pop()
        yield n
        for child in ast.iter_child_nodes(n):
            if isinstance(child, NESTED_SCOPES):
                continue
            todo.append(child)


def _if_returning(func: ast.FunctionDef, want: tuple[object, ...]) -> ast.If | None:
    """The `if` whose direct body returns the literal tuple `want`."""
    for n in ast.walk(func):
        if isinstance(n, ast.If):
            for stmt in n.body:
                if isinstance(stmt, ast.Return) and _const_tuple(stmt.value) == want:
                    return n
    return None


def _dead_branch_tests(func: ast.FunctionDef) -> list[int]:
    """Line numbers of `if`/bool tests that are a falsy constant or an AND/OR
    operand that is a falsy constant — dead-code guards (`if False`,
    `... and False`)."""
    hits: list[int] = []
    for n in ast.walk(func):
        if isinstance(n, ast.If):
            t = n.test
            if _is_falsy_const(t):
                hits.append(t.lineno)
            elif isinstance(t, ast.BoolOp) and any(_is_falsy_const(v) for v in t.values):
                hits.append(t.lineno)
    return hits


def _mode_values(func: ast.FunctionDef) -> list[ast.expr]:
    out: list[ast.expr] = []
    for n in ast.walk(func):
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "mode":
            if n.value is not None:
                out.append(n.value)
        elif isinstance(n, ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "mode":
                    out.append(n.value)
    return out


def _is_blocking_ternary(node: ast.expr) -> bool:
    if not (isinstance(node, ast.IfExp) and isinstance(node.test, ast.Name) and node.test.id == "blocking"):
        return False
    vals = {getattr(node.body, "value", None), getattr(node.orelse, "value", None)}
    return {"blocking", "advisory"} <= vals


def _try_blocks_containing(func: ast.FunctionDef, callee: str) -> list[ast.Try]:
    return [
        n for n in ast.walk(func)
        if isinstance(n, ast.Try) and _calls_named(n.body, callee)
    ]


def _handler_exc_names(handler: ast.ExceptHandler) -> set[str]:
    t = handler.type
    nodes = t.elts if isinstance(t, ast.Tuple) else ([t] if t is not None else [])
    names: set[str] = set()
    for n in nodes:
        if isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
    return names


def _handler_assigns_true(handler: ast.ExceptHandler, name: str) -> bool:
    # `_walk_no_nested` (not ast.walk) so a `name = True` buried in a
    # never-called nested def/lambda doesn't count — same value-flow guard
    # as the early-return scan. Matches `ast.Assign` only (not AnnAssign);
    # a `check_publish_failed: bool = True` would not satisfy this — an
    # intentionally strict constraint, the handler assigns plainly today.
    for stmt in handler.body:
        for n in _walk_no_nested(stmt):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and n.value.value is True:
                if any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
                    return True
    return False


def _check_publish_shape(tree: ast.AST, path: Path) -> list[str]:
    f = _find_funcdef(tree, "_publish_shape")
    if f is None:
        return [f"FAIL: {path} — exactly one `_publish_shape` expected (gate SSOT)"]
    fails: list[str] = []

    dead = _dead_branch_tests(f)
    if dead:
        fails.append(
            f"FAIL: {path} — _publish_shape has dead-code branch test(s) at line(s) "
            f"{dead}; a constant-false guard fakes a reachable gate path"
        )

    # advisory/degraded → ("neutral","COMMENT"), guarded by OR over the two.
    neutral_if = _if_returning(f, ("neutral", "COMMENT"))
    if neutral_if is None:
        fails.append(
            f"FAIL: {path} — no `if` returns ('neutral','COMMENT'); advisory/degraded "
            "must force neutral+COMMENT"
        )
    else:
        test_src = ast.unparse(neutral_if.test)
        if not isinstance(neutral_if.test, ast.BoolOp) or not isinstance(neutral_if.test.op, ast.Or):
            fails.append(
                f"FAIL: {path} — neutral branch is not an OR; it must fire for "
                "advisory mode OR a degraded evaluation"
            )
        if '"advisory"' not in test_src and "'advisory'" not in test_src:
            fails.append(f"FAIL: {path} — neutral branch does not gate on advisory mode")
        if "degraded_reason" not in test_src:
            fails.append(
                f"FAIL: {path} — neutral branch ignores degraded_reason; a degraded "
                "evaluation must force advisory regardless of mode"
            )

    # blocking failure → ("failure","REQUEST_CHANGES"), reachable + on conclusion.
    fail_if = _if_returning(f, ("failure", "REQUEST_CHANGES"))
    if fail_if is None:
        fails.append(
            f"FAIL: {path} — no `if` returns ('failure','REQUEST_CHANGES'); blocking "
            "failure must request changes"
        )
    else:
        test_src = ast.unparse(fail_if.test)
        if _is_falsy_const(fail_if.test):
            fails.append(f"FAIL: {path} — blocking-failure branch is dead (constant test)")
        has_failure_literal = '"failure"' in test_src or "'failure'" in test_src
        if "conclusion" not in test_src or not has_failure_literal:
            fails.append(
                f"FAIL: {path} — blocking-failure branch does not test "
                "`conclusion == 'failure'`"
            )
    return fails


def _kwarg_value(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _check_ssot_flow(
    f: ast.FunctionDef, path: Path, ps_calls: list[ast.Call],
) -> list[str]:
    """The (conclusion, event) tuple from `_publish_shape` must be unpacked
    into two Names, and THOSE names must reach the two publish surfaces —
    conclusion → CheckRunResult(conclusion=), event → _build_review_result(
    event=). Proves the no-drift "single source of truth" property."""
    # Find the assignment `<a>, <b> = _publish_shape(...)`.
    conclusion_name = event_name = None
    for n in ast.walk(f):
        if (
            isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Call)
            and n.value in ps_calls
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Tuple)
            and len(n.targets[0].elts) == 2
            and all(isinstance(e, ast.Name) for e in n.targets[0].elts)
        ):
            conclusion_name = n.targets[0].elts[0].id  # type: ignore[attr-defined]
            event_name = n.targets[0].elts[1].id  # type: ignore[attr-defined]
            break
    if conclusion_name is None:
        return [
            f"FAIL: {path} — _publish_shape result is not unpacked into "
            "`(conclusion, event)`; can't prove single-source-of-truth flow"
        ]
    fails: list[str] = []
    # conclusion → CheckRunResult(conclusion=<conclusion_name>)
    crr = _calls_named(f, "CheckRunResult")
    if not any(
        isinstance(_kwarg_value(c, "conclusion"), ast.Name)
        and _kwarg_value(c, "conclusion").id == conclusion_name  # type: ignore[union-attr]
        for c in crr
    ):
        fails.append(
            f"FAIL: {path} — CheckRunResult(conclusion=) is not the `{conclusion_name}` "
            "from _publish_shape; check-run conclusion may drift from the gate"
        )
    # event → _build_review_result(event=<event_name>)
    brr = _calls_named(f, "_build_review_result")
    if not any(
        isinstance(_kwarg_value(c, "event"), ast.Name)
        and _kwarg_value(c, "event").id == event_name  # type: ignore[union-attr]
        for c in brr
    ):
        fails.append(
            f"FAIL: {path} — _build_review_result(event=) is not the `{event_name}` "
            "from _publish_shape; review event may drift from the gate"
        )
    return fails


def _check_dispatch(tree: ast.AST, path: Path) -> list[str]:
    f = _find_funcdef(tree, "dispatch_code_review")
    if f is None:
        return [f"FAIL: {path} — exactly one `dispatch_code_review` expected"]
    fails: list[str] = []

    # No dead-code guards anywhere (kills `... and False` around post_review etc).
    dead = _dead_branch_tests(f)
    if dead:
        fails.append(
            f"FAIL: {path} — dispatch_code_review has dead-code branch test(s) at "
            f"line(s) {dead}; a constant-false guard hides a skipped publish surface"
        )

    # mode derives from the blocking flag and is never reassigned to a constant.
    params = {a.arg for a in (f.args.args + f.args.kwonlyargs)}
    if "blocking" not in params:
        fails.append(f"FAIL: {path} — dispatch_code_review has no `blocking` param")
    mode_vals = _mode_values(f)
    if not mode_vals:
        fails.append(f"FAIL: {path} — `mode` is never assigned")
    elif not any(_is_blocking_ternary(v) for v in mode_vals):
        fails.append(
            f"FAIL: {path} — `mode` not derived as "
            "`'blocking' if blocking else 'advisory'`"
        )
    if any(isinstance(v, ast.Constant) for v in mode_vals):
        fails.append(
            f"FAIL: {path} — `mode` is reassigned to a constant, overriding the "
            "RepoConfig-derived value"
        )

    # the value handed to _publish_shape must be the `mode` Name, not a literal.
    ps_calls = _calls_named(f, "_publish_shape")
    if not ps_calls:
        fails.append(f"FAIL: {path} — _publish_shape is never called in dispatch")
    for c in ps_calls:
        mode_arg: ast.expr | None = None
        for kw in c.keywords:
            if kw.arg == "mode":
                mode_arg = kw.value
        if mode_arg is None and len(c.args) >= 2:
            mode_arg = c.args[1]
        if not (isinstance(mode_arg, ast.Name) and mode_arg.id == "mode"):
            fails.append(
                f"FAIL: {path} — _publish_shape called with a non-`mode` argument at "
                f"line {c.lineno}; the gate must consume the derived mode"
            )

    # SSOT no-drift: the (conclusion, event) pair _publish_shape returns must
    # be the SAME values published — conclusion into CheckRunResult(conclusion=),
    # event into _build_review_result(event=). Without this, a mutation could
    # recompute `event` independently and the two surfaces would drift.
    fails.extend(_check_ssot_flow(f, path, ps_calls))

    # check-run + review on independent surfaces.
    check_tries = _try_blocks_containing(f, "post_check_run")
    review_tries = _try_blocks_containing(f, "post_review")
    if not check_tries:
        fails.append(f"FAIL: {path} — post_check_run is not wrapped in try/except")
    if not review_tries:
        fails.append(
            f"FAIL: {path} — post_review not wrapped in its own try (independent surface)"
        )

    for tnode in check_tries:
        # handler types must be able to catch a wire 5xx
        for handler in tnode.handlers:
            names = _handler_exc_names(handler)
            if not (names & ALLOWED_WIRE_EXC):
                fails.append(
                    f"FAIL: {path} — check-run except catches {sorted(names) or 'nothing'}; "
                    f"must catch a wire exception {sorted(ALLOWED_WIRE_EXC)}"
                )
            # must NOT early-return (would skip the independent review post)
            if any(
                isinstance(n, ast.Return)
                for stmt in handler.body for n in _walk_no_nested(stmt)
            ):
                fails.append(
                    f"FAIL: {path} — check-run except returns early; a check-run "
                    "failure would skip the independent review post"
                )
            # must RECORD the failure (Assign True, not a logged string)
            if not _handler_assigns_true(handler, "check_publish_failed"):
                fails.append(
                    f"FAIL: {path} — check-run except does not set "
                    "`check_publish_failed = True`"
                )

    for tnode in review_tries:
        for handler in tnode.handlers:
            names = _handler_exc_names(handler)
            if not (names & ALLOWED_WIRE_EXC):
                fails.append(
                    f"FAIL: {path} — review except catches {sorted(names) or 'nothing'}; "
                    f"must catch a wire exception"
                )

    # never-raise: fetch/parse degrade fallback present.
    if not _calls_named(f, "_publish_degraded"):
        fails.append(
            f"FAIL: {path} — no _publish_degraded fallback; fetch/parse errors must "
            "degrade to advisory-neutral, not raise"
        )

    # result rollup consults BOTH independent publish surfaces.
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
        f"OK: _publish_shape gate (reachable branches + mode flow) + dual-publish "
        f"independence verified in {len(DISPATCH_PATHS)} mirrored dispatch module(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
