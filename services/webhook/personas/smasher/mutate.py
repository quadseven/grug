# MIRRORED — sibling at services/api/personas/smasher/mutate.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Diff-scoped mutation engine for the Smasher persona (#469, ADR-0013).

PURE + no IO. `generate_mutants(source, target_lines)` parses a Python file
with `ast`, finds mutable nodes whose line is in the diff-added set, and yields
one `Mutant` per applicable mutation. Each mutant carries the FULL mutated
source, produced via `ast.unparse`, so it is ALWAYS syntactically valid — a
mutant that failed to parse would be a false "killed" and inflate the coverage
confidence the whole tracer exists to measure (ADR-0013).

Operators (deliberately small — the tracer scope, #346 P3.1):
  - comparison-flip : `==`<->`!=`, `<`<->`<=`, `>`<->`>=`  (boundary-sensitive)
  - boundary        : integer literal N -> N+1
  - boolean         : `True`<->`False`, `and`<->`or`
  - return-value    : `return <expr>` -> `return None`

Diff-scoping: only nodes on a line in `target_lines` (the diff's added-line
set) are mutated — a PR review measures the coverage of what the PR INTRODUCES,
not the whole file. Deterministic order (AST walk order); the caller enforces
the mutant cap.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class Mutant:
    """One applied mutation. `source` is the full mutated file (valid Python).
    `original`/`mutated` are short human-readable before/after tokens for the
    finding message + the survived-mutant reproducer. `operator` names the
    mutation class; `(file, line)` anchor it to the added diff line."""

    file: str
    line: int
    operator: str
    original: str
    mutated: str
    source: str


# One mutation site: the node's index in a deterministic `ast.walk` order, its
# operator label, human before/after, and a pure in-place mutation applied to
# the SAME-position node in a deep copy of the tree.
@dataclass(frozen=True, slots=True)
class _Site:
    node_index: int
    line: int
    operator: str
    original: str
    mutated: str
    apply: Callable[[ast.AST], None]


_CMP_FLIP: dict[type[ast.cmpop], tuple[type[ast.cmpop], str, str]] = {
    ast.Eq: (ast.NotEq, "==", "!="),
    ast.NotEq: (ast.Eq, "!=", "=="),
    ast.Lt: (ast.LtE, "<", "<="),
    ast.LtE: (ast.Lt, "<=", "<"),
    ast.Gt: (ast.GtE, ">", ">="),
    ast.GtE: (ast.Gt, ">=", ">"),
}


def _sites_for(node: ast.AST, index: int) -> list[_Site]:
    """Zero or more mutation sites rooted at `node` (already known to be on a
    target line). Each site's `apply` mutates the positionally-identical node
    in a fresh deep copy of the tree — never `node` itself."""
    line = getattr(node, "lineno", 0)
    out: list[_Site] = []

    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        flip = _CMP_FLIP.get(type(node.ops[0]))
        if flip is not None:
            new_op_cls, before, after = flip

            def _apply_cmp(n: ast.AST, _cls=new_op_cls) -> None:
                assert isinstance(n, ast.Compare)
                n.ops[0] = _cls()

            out.append(_Site(index, line, "comparison-flip", before, after, _apply_cmp))

    elif isinstance(node, ast.Constant):
        # bool is a subclass of int — check it FIRST so True/False don't become
        # boundary mutants (True+1 == 2 would be a nonsense, non-bool mutation).
        if isinstance(node.value, bool):
            flipped = not node.value

            def _apply_bool_const(n: ast.AST, _v=flipped) -> None:
                assert isinstance(n, ast.Constant)
                n.value = _v

            out.append(
                _Site(index, line, "boolean", str(node.value), str(flipped), _apply_bool_const)
            )
        elif isinstance(node.value, int):
            bumped = node.value + 1

            def _apply_boundary(n: ast.AST, _v=bumped) -> None:
                assert isinstance(n, ast.Constant)
                n.value = _v

            out.append(
                _Site(index, line, "boundary", str(node.value), str(bumped), _apply_boundary)
            )

    elif isinstance(node, ast.BoolOp):
        is_and = isinstance(node.op, ast.And)
        new_cls = ast.Or if is_and else ast.And

        def _apply_boolop(n: ast.AST, _cls=new_cls) -> None:
            assert isinstance(n, ast.BoolOp)
            n.op = _cls()

        out.append(
            _Site(
                index, line, "boolean",
                "and" if is_and else "or",
                "or" if is_and else "and",
                _apply_boolop,
            )
        )

    elif isinstance(node, ast.Return) and node.value is not None:
        # Skip an already-`return None` (Constant None) — mutating it is a no-op.
        if not (isinstance(node.value, ast.Constant) and node.value.value is None):

            def _apply_return(n: ast.AST) -> None:
                assert isinstance(n, ast.Return)
                n.value = ast.Constant(value=None)

            out.append(_Site(index, line, "return-value", "return <expr>", "return None", _apply_return))

    return out


def generate_mutants(
    source: str,
    *,
    file: str,
    target_lines: frozenset[int],
    cap: int | None = None,
) -> tuple[Mutant, ...]:
    """Yield syntactically-valid mutants for the added lines of one file.

    `target_lines` is the diff's added-line set (1-based). `cap` bounds the
    mutant count (deterministic prefix). Returns `()` — never raises — when the
    source doesn't parse or has no mutable target site (fail-safe: a Trial that
    can't mutate simply reports nothing)."""
    if not target_lines:
        return ()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()

    nodes = list(ast.walk(tree))
    sites: list[_Site] = []
    for index, node in enumerate(nodes):
        if getattr(node, "lineno", None) in target_lines:
            sites.extend(_sites_for(node, index))

    if cap is not None:
        sites = sites[:cap]

    mutants: list[Mutant] = []
    for site in sites:
        tree_copy = copy.deepcopy(tree)
        target = list(ast.walk(tree_copy))[site.node_index]
        site.apply(target)
        ast.fix_missing_locations(tree_copy)
        try:
            mutated_source = ast.unparse(tree_copy)
        except Exception:  # noqa: BLE001 — a non-unparseable mutation is dropped, never raised
            continue
        mutants.append(
            Mutant(
                file=file,
                line=site.line,
                operator=site.operator,
                original=site.original,
                mutated=site.mutated,
                source=mutated_source,
            )
        )
    return tuple(mutants)
