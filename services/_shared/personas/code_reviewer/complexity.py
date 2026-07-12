"""Deterministic complexity source for the Elder review (#532).

A pure, no-LLM finding source: cyclomatic + cognitive complexity over the
Python functions a diff actually touches. A function above a per-repo cap is a
high-signal, low-false-positive finding the review model routinely misses (an
LLM anchors on correctness, not on "this branch thicket is unmaintainable").

Additive + language-scoped: only files the diff changed AND that parse as
Python are scanned; anything else yields nothing (never blocks a review). Each
finding is anchored to a REAL changed line inside the function, so it passes the
same anti-hallucination invariant the LLM findings do, and merges into the Elder
evaluation via `with_extra_findings`.

Pure: (hunks, file_contents, caps) in, Findings out. No IO.
"""

from __future__ import annotations

import ast
import os

from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import Finding

# Defaults chosen from common linter conventions (radon/flake8-cognitive):
# cyclomatic > 15 is "high", cognitive > 25 is "hard to follow". Env-tunable
# for a global dial; a per-repo override is a follow-up (config key).
_DEFAULT_CYCLOMATIC_CAP = int(os.getenv("GRUG_COMPLEXITY_CYCLO_CAP", "15"))
_DEFAULT_COGNITIVE_CAP = int(os.getenv("GRUG_COMPLEXITY_COGNITIVE_CAP", "25"))

_RULE = "high-complexity"


def _changed_lines(hunk: DiffHunk) -> set[int]:
    """New-side line numbers ADDED in this hunk (unified-diff walk, mirrors
    sast._added_lines but returns only the numbers)."""
    out: set[int] = set()
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith("@@") or raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            out.add(lineno)
            lineno += 1
        elif raw.startswith("-"):
            continue
        else:
            lineno += 1
    return out


def _changed_by_file(hunks: tuple[DiffHunk, ...]) -> dict[str, set[int]]:
    by_file: dict[str, set[int]] = {}
    for h in hunks:
        by_file.setdefault(h.file_path, set()).update(_changed_lines(h))
    return by_file


# --- complexity metrics (pure over an AST subtree) --------------------------

# Nodes that each add one independent path (cyclomatic).
_CYCLO_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
    ast.With, ast.AsyncWith, ast.IfExp, ast.comprehension, ast.Assert,
)


def cyclomatic_complexity(func: ast.AST) -> int:
    """McCabe cyclomatic complexity of one function subtree: 1 + decision
    points. Each boolean operator adds (operands - 1) sub-paths; `match`
    contributes one per non-wildcard case."""
    score = 1
    for node in ast.walk(func):
        if isinstance(node, _CYCLO_NODES):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        elif isinstance(node, ast.match_case):
            # a bare `case _:` (wildcard) is the default arm, not a branch.
            if not isinstance(node.pattern, ast.MatchAs) or node.pattern.pattern is not None:
                score += 1
    return score


_COGNITIVE_NESTERS = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
)


def cognitive_complexity(func: ast.AST) -> int:
    """Cognitive complexity (Sonar-style, simplified): control-flow structures
    cost 1 + their nesting depth; boolean-operator sequences cost 1 each;
    nested function defs increase depth but a def itself is free. Measures how
    hard the code is to FOLLOW, which cyclomatic alone misses (deep nesting
    reads far worse than a flat switch of the same branch count)."""

    def walk(node: ast.AST, depth: int) -> int:
        total = 0
        for child in ast.iter_child_nodes(node):
            inc = 0
            child_depth = depth
            if isinstance(child, _COGNITIVE_NESTERS):
                inc = 1 + depth
                child_depth = depth + 1
            elif isinstance(child, ast.BoolOp):
                inc = 1
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_depth = depth + 1  # nesting rises, but the def is free
            total += inc + walk(child, child_depth)
        return total

    return walk(func, 0)


def _func_line_span(func: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
    start = func.lineno
    end = getattr(func, "end_lineno", None) or max(
        (getattr(n, "lineno", start) for n in ast.walk(func)), default=start,
    )
    return start, end


def scan_complexity(
    hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str],
    *,
    cyclomatic_cap: int | None = None,
    cognitive_cap: int | None = None,
) -> tuple[Finding, ...]:
    """One advisory Finding per changed Python function over a cap. Pure.

    Only functions whose line span overlaps a changed line are scanned (a diff
    that just touches one method of a huge class doesn't flag the others). The
    finding anchors on the SMALLEST changed line inside the function so it is
    diff-anchored. `file_contents` is the #336 full-file-at-head fetch (needed
    to see the WHOLE function, not just the diff hunk)."""
    cyclo_cap = cyclomatic_cap if cyclomatic_cap is not None else _DEFAULT_CYCLOMATIC_CAP
    cog_cap = cognitive_cap if cognitive_cap is not None else _DEFAULT_COGNITIVE_CAP
    changed = _changed_by_file(hunks)
    findings: list[Finding] = []

    for path, changed_lines in changed.items():
        if not path.endswith(".py") or not changed_lines:
            continue
        source = file_contents.get(path)
        if not source:
            continue  # no full-file content -> can't measure whole functions
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue  # unparseable (partial file, py2, generated) -> skip

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            start, end = _func_line_span(node)
            touched = {ln for ln in changed_lines if start <= ln <= end}
            if not touched:
                continue
            cyclo = cyclomatic_complexity(node)
            cog = cognitive_complexity(node)
            if cyclo <= cyclo_cap and cog <= cog_cap:
                continue
            over = []
            if cyclo > cyclo_cap:
                over.append(f"cyclomatic {cyclo} (cap {cyclo_cap})")
            if cog > cog_cap:
                over.append(f"cognitive {cog} (cap {cog_cap})")
            findings.append(
                Finding(
                    file=path,
                    line=min(touched),
                    severity="medium",  # advisory: never blocks on its own
                    rule_name=_RULE,
                    message=(
                        f"Function `{node.name}` too tangled -- "
                        f"{', '.join(over)}. Grug say: break into smaller pieces "
                        f"so next hunter read it without getting lost."
                    ),
                    suggestion=None,
                    effort="heavy-lift",
                )
            )
    return tuple(findings)
