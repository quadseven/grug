"""Mutation-engine tests (#469, Smasher Trial).

The engine is pure + diff-scoped: `generate_mutants(source, target_lines)`
yields one syntactically-valid `Mutant` per applicable AST mutation on an
ADDED line. These tests lock the operator set, the diff-scoping, the mutant
cap, and the syntactic-validity invariant (a mutant that doesn't parse would
be a false "killed").
"""

from __future__ import annotations

import ast

from personas.smasher.mutate import Mutant, generate_mutants


def _lines(source: str) -> frozenset[int]:
    """All 1-based line numbers of a snippet (a whole-file target set)."""
    return frozenset(range(1, source.count("\n") + 2))


def _all_valid(mutants):
    for m in mutants:
        # The invariant that makes mutation testing sound: every mutant parses.
        ast.parse(m.source)


def test_comparison_flip_lt_to_lte():
    src = "def f(x):\n    return x > 0\n"
    mutants = generate_mutants(src, file="m.py", target_lines=_lines(src))
    ops = {m.operator for m in mutants}
    assert "comparison-flip" in ops
    flip = next(m for m in mutants if m.operator == "comparison-flip")
    # `x > 0` boundary-flips to `x >= 0` — the classic off-by-one gap.
    assert ">=" in flip.source
    _all_valid(mutants)


def test_boundary_int_literal():
    src = "def f():\n    return 10\n"
    mutants = generate_mutants(src, file="m.py", target_lines=_lines(src))
    boundary = [m for m in mutants if m.operator == "boundary"]
    assert boundary, "expected a boundary mutant on the int literal 10"
    assert any("11" in m.source for m in boundary)
    _all_valid(mutants)


def test_boolean_literal_flip():
    src = "def f():\n    return True\n"
    mutants = generate_mutants(src, file="m.py", target_lines=_lines(src))
    boolean = [m for m in mutants if m.operator == "boolean"]
    assert boolean
    assert any("False" in m.source for m in boolean)
    _all_valid(mutants)


def test_boolop_and_to_or():
    src = "def f(a, b):\n    return a and b\n"
    mutants = generate_mutants(src, file="m.py", target_lines=_lines(src))
    boolean = [m for m in mutants if m.operator == "boolean"]
    assert any(" or " in m.source for m in boolean)
    _all_valid(mutants)


def test_return_value_replacement():
    src = "def f(x):\n    return x + 1\n"
    mutants = generate_mutants(src, file="m.py", target_lines=_lines(src))
    rv = [m for m in mutants if m.operator == "return-value"]
    assert rv, "expected a return-value mutant"
    assert any("return None" in m.source for m in rv)
    _all_valid(mutants)


def test_diff_scoped_only_added_lines():
    # Line 2 is the only "added" line; the comparison on line 4 must NOT mutate.
    src = (
        "def f(x):\n"          # 1
        "    y = x == 0\n"      # 2  <- target
        "    if x < 5:\n"       # 3
        "        return x > 9\n"  # 4
        "    return y\n"        # 5
    )
    mutants = generate_mutants(src, file="m.py", target_lines=frozenset({2}))
    assert mutants, "line 2 has a mutable comparison"
    assert all(m.line == 2 for m in mutants), "only line 2 was added"
    _all_valid(mutants)


def test_no_targets_no_mutants():
    src = "def f(x):\n    return x > 0\n"
    assert generate_mutants(src, file="m.py", target_lines=frozenset()) == ()


def test_mutant_cap_enforced():
    src = "def f(a, b, c):\n    return a == 1 or b == 2 or c == 3\n"
    mutants = generate_mutants(
        src, file="m.py", target_lines=_lines(src), cap=2
    )
    assert len(mutants) == 2


def test_syntax_error_source_yields_no_mutants():
    # A file that doesn't parse can't be mutated — degrade to nothing, never raise.
    assert generate_mutants("def (:\n", file="m.py", target_lines=frozenset({1})) == ()


def test_mutant_is_frozen_and_carries_provenance():
    src = "def f(x):\n    return x > 0\n"
    m = generate_mutants(src, file="m.py", target_lines=_lines(src))[0]
    assert isinstance(m, Mutant)
    assert m.file == "m.py"
    assert m.original and m.mutated  # human-readable before/after
    assert m.line >= 1
