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
from dataclasses import dataclass

from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import Finding

# Defaults chosen from common linter conventions (radon/flake8-cognitive):
# cyclomatic > 15 is "high", cognitive > 25 is "hard to follow". Env-tunable
# for a global dial; a per-repo override is a follow-up (config key).
def _cap_env(name: str, default: int) -> int:
    """Env-tunable cap that degrades to the default on a non-numeric value -
    an operator typo must not crash module import (and thus the whole
    dispatch chain), it just falls back."""
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


_DEFAULT_CYCLOMATIC_CAP = _cap_env("GRUG_COMPLEXITY_CYCLO_CAP", 15)
_DEFAULT_COGNITIVE_CAP = _cap_env("GRUG_COMPLEXITY_COGNITIVE_CAP", 25)

# How much WORSE an already-over-cap function must get before it is worth an
# inline comment. Small churn inside a tangled function is normal maintenance;
# only a real step change is this PR's doing. Tunable, but not per-repo config
# - a threshold nobody can explain is a threshold nobody trusts.
_REGRESSION_CYCLO = _cap_env("GRUG_COMPLEXITY_REGRESSION_CYCLO", 3)
_REGRESSION_COG = _cap_env("GRUG_COMPLEXITY_REGRESSION_COG", 5)

_RULE = "high-complexity"


@dataclass(frozen=True)
class SuppressedComplexity:
    """A function the regression gate held back (#781): over cap, but this PR
    did not make it worse. Exists so the caller can log a count distinct from
    published findings - the #707 scoreboard has no way to measure how many
    markings the gate removes without one."""

    file: str
    function: str
    cyclomatic: int
    cognitive: int
    base_cyclomatic: int
    base_cognitive: int


@dataclass(frozen=True)
class ComplexityScan:
    findings: tuple[Finding, ...]
    suppressed: tuple[SuppressedComplexity, ...]


def _changed_lines(hunk: DiffHunk) -> set[int]:
    """New-side line numbers ADDED in this hunk (unified-diff walk, mirrors
    sast._added_lines but returns only the numbers)."""
    out: set[int] = set()
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith(("@@", "+++", "---")):
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


def _score_functions(source: str) -> dict[str, tuple[int, int]]:
    """`{function name: (cyclomatic, cognitive)}` for one file, or {} if it
    does not parse. Keyed by NAME, not line span, because the whole point is
    to compare across two revisions where line numbers have moved."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    out: dict[str, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Same bare name twice in a file (an overload, a method on two
            # classes): keep the WORST base score. Comparing against the
            # gentler twin would manufacture a fake regression.
            prev = out.get(node.name)
            cur = (cyclomatic_complexity(node), cognitive_complexity(node))
            out[node.name] = cur if prev is None else (
                max(prev[0], cur[0]), max(prev[1], cur[1])
            )
    return out


def _is_regression(
    base_cyclo: int | None, base_cog: int | None,
    cyclo: int, cog: int, cyclo_cap: int, cog_cap: int,
) -> bool:
    """Did THIS PR cause the over-cap state, or was it already there?

    No base (fetch failed, unparseable, or new file) -> True, i.e. fall back
    to the old absolute behaviour. Going SILENT on a missing base would hide
    real regressions; falling back only restores the previous noise level.
    """
    if base_cyclo is None or base_cog is None:
        return True
    crossed = (base_cyclo <= cyclo_cap < cyclo) or (base_cog <= cog_cap < cog)
    worsened = (
        cyclo - base_cyclo >= _REGRESSION_CYCLO
        or cog - base_cog >= _REGRESSION_COG
    )
    return crossed or worsened


def _delta_phrase(
    base_cyclo: int | None, base_cog: int | None,
    cyclo: int, cog: int, had_base: bool,
) -> str:
    """What the PR actually did, in words - the part that makes the finding
    arguable-with instead of a bare threshold assertion."""
    if base_cyclo is not None and base_cog is not None:
        return (
            f" This PR moved it {base_cyclo}->{cyclo} cyclomatic, "
            f"{base_cog}->{cog} cognitive."
        )
    return " New function at this size." if had_base else ""


def _scan_one_file(
    path: str, source: str, changed_lines: set[int],
    base: dict[str, tuple[int, int]], had_base: bool,
    cyclo_cap: int, cog_cap: int,
) -> tuple[list[Finding], list[SuppressedComplexity]]:
    """Findings for one file. Split out of `scan_complexity` because that
    function tripped its OWN cap once the regression gate landed (19/30 vs
    16/26 on main) - a complexity rule that cannot pass its own rule is not
    one anybody will take seriously."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return [], []  # unparseable (partial file, py2, generated) -> skip

    out: list[Finding] = []
    suppressed: list[SuppressedComplexity] = []
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

        base_cyclo, base_cog = base.get(node.name, (None, None))
        if not _is_regression(base_cyclo, base_cog, cyclo, cog, cyclo_cap, cog_cap):
            # Only a function WITH a base can land here (no base -> _is_regression
            # always True), so base_cyclo/base_cog are never None here.
            assert base_cyclo is not None and base_cog is not None
            suppressed.append(SuppressedComplexity(
                file=path, function=node.name,
                cyclomatic=cyclo, cognitive=cog,
                base_cyclomatic=base_cyclo, base_cognitive=base_cog,
            ))
            continue

        over = []
        if cyclo > cyclo_cap:
            over.append(f"cyclomatic {cyclo} (cap {cyclo_cap})")
        if cog > cog_cap:
            over.append(f"cognitive {cog} (cap {cog_cap})")
        delta = _delta_phrase(base_cyclo, base_cog, cyclo, cog, had_base)
        out.append(Finding(
            file=path,
            line=min(touched),
            severity="medium",  # advisory: never blocks on its own
            rule_name=_RULE,
            message=(
                f"Function `{node.name}` too tangled -- "
                f"{', '.join(over)}.{delta} Grug say: break into smaller "
                f"pieces so next hunter read it without getting lost."
            ),
            suggestion=None,
            effort="heavy-lift",
        ))
    return out, suppressed


def scan_complexity_full(
    hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str],
    *,
    cyclomatic_cap: int | None = None,
    cognitive_cap: int | None = None,
    base_contents: dict[str, str] | None = None,
) -> ComplexityScan:
    """Advisory Findings for functions THIS PR pushed over a complexity cap,
    plus the functions the regression gate held back (#781).

    Only functions whose span overlaps a changed line are scanned, and the
    finding anchors on the smallest changed line inside the function so it
    stays diff-anchored. `file_contents` is the #336 full-file-at-head fetch.

    REGRESSION GATE. Without a base, a function is flagged for its HEAD score,
    so touching one line of an already-tangled function reported its entire
    pre-existing debt as if this PR caused it. Measured: PR #766 was told
    `poller_handler.handler` was cyclomatic 30 / cognitive 53 - exactly main's
    baseline, on a function it had not touched (#767). An adversarial audit of
    the last 120 findings put this rule at 85 of them, 71% of all output, with
    most replies saying "pre-existing". With `base_contents`, only a crossed
    cap, a material worsening, or a new over-cap function is published; the
    rest come back as `suppressed` so the caller can log a count (#781) -
    the #707 scoreboard otherwise has no way to measure how many markings
    this gate removes.

    Pure: no IO.
    """
    cyclo_cap = cyclomatic_cap if cyclomatic_cap is not None else _DEFAULT_CYCLOMATIC_CAP
    cog_cap = cognitive_cap if cognitive_cap is not None else _DEFAULT_COGNITIVE_CAP
    base_scores = {
        path: _score_functions(src) for path, src in (base_contents or {}).items()
    }
    findings: list[Finding] = []
    suppressed: list[SuppressedComplexity] = []
    for path, changed_lines in _changed_by_file(hunks).items():
        source = file_contents.get(path)
        if not path.endswith(".py") or not changed_lines or not source:
            continue
        file_findings, file_suppressed = _scan_one_file(
            path, source, changed_lines,
            base_scores.get(path, {}), bool(base_scores),
            cyclo_cap, cog_cap,
        )
        findings.extend(file_findings)
        suppressed.extend(file_suppressed)
    return ComplexityScan(findings=tuple(findings), suppressed=tuple(suppressed))


def scan_complexity(
    hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str],
    *,
    cyclomatic_cap: int | None = None,
    cognitive_cap: int | None = None,
    base_contents: dict[str, str] | None = None,
) -> tuple[Finding, ...]:
    """Findings-only view of `scan_complexity_full`, kept for callers that
    don't need suppression visibility. Pure: no IO."""
    return scan_complexity_full(
        hunks, file_contents,
        cyclomatic_cap=cyclomatic_cap, cognitive_cap=cognitive_cap,
        base_contents=base_contents,
    ).findings
